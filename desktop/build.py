"""My TV Windows EXE 构建脚本。

产物：``apk/Windows版/MyTV.exe``（单文件、无控制台窗口、双击即用）。

与 launcher.py 的打包契约（两者必须一致）：
  * 数据解压到 ``sys._MEIPASS/desktop/backend.zip`` 与
    ``sys._MEIPASS/desktop/mytv_migration_ca.pem``；
  * backend.zip 内含 tools/backend-src 的后端模块 + 完整 www 前端；
  * hls.js@1.5.13 已下载并打进 zip，index.html 引用改成本地相对路径。

用法：
    python desktop/build.py                # 完整构建（需要联网取 hls.js）
    python desktop/build.py --bundle-only  # 只产出 backend.zip / 图标，不跑 PyInstaller
    python desktop/build.py --offline      # 复用已缓存的 hls.js，不再联网

刻意不调用 tools/build_backend_zip.py 的 main()：那个函数会覆写 Android assets
里的热更包，Windows 构建不该有那个副作用。本文件只读源码，自己打 zip。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional


DESKTOP_DIR = Path(__file__).resolve().parent
ROOT = DESKTOP_DIR.parent
BACKEND_SRC = ROOT / "tools" / "backend-src"
PEM_SOURCE = ROOT / "android" / "app" / "src" / "main" / "res" / "raw" / "mytv_migration_ca.pem"
LOGO_SOURCE = BACKEND_SRC / "www" / "assets" / "logo.png"
OUT_DIR = ROOT / "apk" / "Windows版"
EXE_NAME = "MyTV"

# launcher.py 期望的包内路径；改这里必须同步改 launcher.py。
BUNDLE_DIR_NAME = "desktop"
BACKEND_ZIP_NAME = "backend.zip"
PEM_NAME = "mytv_migration_ca.pem"

RUNTIME_DEPENDENCIES = ("webview", "quickjs", "PyInstaller")
PINNED_VERSIONS = {"webview": "6.2.1", "quickjs": "1.19.4", "pyinstaller": "6.19.0"}

# ZIP 条目时间固定，相同源码重复构建得到相同 SHA-256（与 Android 热更包同思路）。
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


class HlsDownloadError(RuntimeError):
    """取不到 / 取错 hls.js 时抛出。宁可构建失败，也不产出无 HLS 能力的 EXE。"""


@dataclass(frozen=True)
class HlsSpec:
    url: str
    version: str
    sha256: str
    size: int


@dataclass(frozen=True)
class HlsAsset:
    path: Path
    version: str
    sha256: str
    size: int


# 与 tools/backend-src/www/index.html 里那条 CDN 引用同一个固定版本。
# sha256/size 为实测值，构建期逐字节校验，防止 CDN 内容被替换或被劫持。
HLS_SPEC = HlsSpec(
    url="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js",
    version="1.5.13",
    sha256="e5753b10fc65eae35f21883e684d22c43ce66de968d35166792585b9b8a3f1a2",
    size=413181,
)

HLS_ASSET_NAME = "assets/hls.min.js"
_HLS_CDN_RE = re.compile(
    r"""(<script\b[^>]*\bsrc=["'])https://cdn\.jsdelivr\.net/npm/hls\.js@[^"']+/hls\.min\.js(?:\?[^"']*)?(["'])""",
    re.IGNORECASE,
)
_HLS_VERSION_RE = re.compile(r'\{key:"version",get:function\(\)\{return"([0-9][^"]*)"\}\}')


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # 按文件路径加载 tools/ 下的模块时，build_backend_zip.py 会退化成
    # `from build_web_assets import ...`，需要 tools/ 在 sys.path 上。
    tools_dir = str(path.parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_android_backend_files() -> tuple[str, ...]:
    """复用 tools/build_backend_zip.py 的 REPL 清单，避免两份白名单漂移。"""
    return tuple(_load_module("mytv_build_backend_zip", ROOT / "tools" / "build_backend_zip.py").REPL)


# REPL（与 Android 热更包同一份清单）+ Windows 额外需要的 quickjs 兜底。
# decrypt_python.py 在 Android 侧不进包（那里走 :8091 的 QuickJS 服务），
# 但 Windows 没有 node，proxy.decrypt_page() 会 import 它，必须带上。
_ANDROID_FILES = _load_android_backend_files()
BACKEND_FILES: tuple[str, ...] = tuple(dict.fromkeys(_ANDROID_FILES + ("decrypt_python.py",)))


def backend_hidden_imports() -> tuple[str, ...]:
    """收集从解压后源码动态导入时 PyInstaller 无法静态看见的依赖。

    自有模块作为源码放在 backend.zip 中，不能再打进 PYZ，否则升级后可能
    被冻结的旧模块抢先导入。只收标准库和明确使用的第三方运行库。
    """
    names: set[str] = set()
    for filename in BACKEND_FILES:
        if not filename.endswith(".py"):
            continue
        tree = ast.parse((BACKEND_SRC / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
                if node.module == "PIL":
                    names.update("PIL." + alias.name for alias in node.names)
    allowed = set(sys.stdlib_module_names) | {"PIL", "urllib3", "quickjs"}
    return tuple(sorted(name for name in names
                        if name.split(".", 1)[0] in allowed
                        and name != "__future__"))


def _verify_hls_version(payload: bytes) -> str:
    """从 hls.js 自身体内读版本号，顺带挡住 404 HTML / 网关错误页。"""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HlsDownloadError("hls.js 不是 UTF-8 文本，可能取到了错误页") from exc
    if text.lstrip()[:1] == "<":
        raise HlsDownloadError("hls.js 取到的是 HTML 页面，CDN 可能返回了错误")
    match = _HLS_VERSION_RE.search(text)
    if match is None:
        raise HlsDownloadError("hls.js 里找不到 version 字段，内容与预期不符")
    return match.group(1)


def fetch_hls(spec: HlsSpec, cache_path: Path,
              opener: Optional[Callable[[str], bytes]] = None) -> HlsAsset:
    """取回固定 sha256 的 hls.js；缓存命中也要校验，坏缓存不静默使用。"""
    if cache_path.is_file():
        payload = cache_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() == spec.sha256:
            return HlsAsset(cache_path, spec.version, spec.sha256, len(payload))
        print("缓存的 hls.js 校验失败，重新下载：%s" % cache_path)

    if opener is None:
        def opener(url: str) -> bytes:
            return urllib.request.urlopen(url, timeout=60).read()

    try:
        payload = opener(spec.url)
    except Exception as exc:  # 网络/证书/DNS 一律视为构建失败
        raise HlsDownloadError(
            "下载 hls.js@%s 失败：%r。请联网重试，或先在联网机器上跑一次让缓存生效"
            % (spec.version, exc)) from exc

    digest = hashlib.sha256(payload).hexdigest()
    if len(payload) != spec.size or digest != spec.sha256:
        raise HlsDownloadError(
            "hls.js@%s 校验不通过：期望 %d bytes / %s，实际 %d bytes / %s"
            % (spec.version, spec.size, spec.sha256, len(payload), digest))
    version = _verify_hls_version(payload)
    if version != spec.version:
        raise HlsDownloadError("hls.js 版本不符：期望 %s，实际 %s" % (spec.version, version))

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(payload)
    return HlsAsset(cache_path, version, digest, len(payload))


def _localize_hls(html: str) -> str:
    """把 index.html 里的 CDN 引用换成 zip 内相对路径（只改生成物，不动源码）。"""
    localized, count = _HLS_CDN_RE.subn(r"\1%s\2" % HLS_ASSET_NAME, html)
    if count != 1:
        raise HlsDownloadError(
            "index.html 里的 hls.js CDN 引用匹配到 %d 处（应为 1），请检查 www/index.html" % count)
    return localized


def _write_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


@dataclass(frozen=True)
class BundleResult:
    zip_path: Path
    hls: HlsAsset
    hls_cache: Path
    entries: tuple[str, ...]


def build_backend_zip(output: Path, *, allow_network: bool = True,
                      hls_cache: Optional[Path] = None,
                      backend_source: Path = BACKEND_SRC) -> BundleResult:
    """从 backend-src 源码打一个干净的后端 zip（只读源码，不改 Android assets）。"""
    source = Path(backend_source)
    for name in BACKEND_FILES:
        if not (source / name).is_file():
            raise SystemExit("缺少后端源文件：%s" % (source / name))

    cache = Path(hls_cache) if hls_cache else _default_hls_cache()
    if cache.is_file() or allow_network:
        hls = fetch_hls(HLS_SPEC, cache_path=cache)
    else:
        raise HlsDownloadError(
            "--offline 模式需要已缓存的 hls.js：%s（先联网构建一次）" % cache)

    web_dist = Path(tempfile.mkdtemp(prefix="mytv-web-dist-"))
    try:
        _build_web_assets(source / "www", web_dist)
        index_html = (web_dist / "index.html").read_text(encoding="utf-8")
        localized = _localize_hls(index_html)

        output.parent.mkdir(parents=True, exist_ok=True)
        entries: list[str] = []
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in BACKEND_FILES:
                _write_entry(archive, name, (source / name).read_bytes())
                entries.append(name)
            for full in sorted(path for path in web_dist.rglob("*") if path.is_file()):
                relative = "www/" + full.relative_to(web_dist).as_posix()
                if relative == "www/index.html":
                    _write_entry(archive, relative, localized.encode("utf-8"))
                elif relative == "www/" + HLS_ASSET_NAME:
                    continue  # 下面用带校验的缓存字节写入
                else:
                    _write_entry(archive, relative, full.read_bytes())
                entries.append(relative)
            _write_entry(archive, "www/" + HLS_ASSET_NAME, hls.path.read_bytes())
            entries.append("www/" + HLS_ASSET_NAME)
            # Unversioned 源文件也保留，与 Android 热更包一致：tools/调试脚本和
            # 旧本地链接仍能直接取 www/app.js、www/style.css。
            for name in ("app.js", "style.css"):
                _write_entry(archive, "www/" + name, (source / "www" / name).read_bytes())
                entries.append("www/" + name)
    finally:
        shutil.rmtree(web_dist, ignore_errors=True)
    return BundleResult(output, hls, cache, tuple(entries))


def _default_hls_cache() -> Path:
    # 放在 gitignored 的 apk/Windows版/build 旁边，仓库外、跨构建复用。
    return OUT_DIR / "build" / "cache" / "hls.min.js"


def _build_web_assets(source: Path, output: Path) -> None:
    """复用 tools/build_web_assets.py 生成内容寻址的 www（index/app.js/style.css）。"""
    _load_module("mytv_build_web_assets", ROOT / "tools" / "build_web_assets.py").build_assets(source, output)


def write_icon(icon_path: Path, logo_path: Path = LOGO_SOURCE) -> Path:
    """logo.png -> 多档 Windows .ico（16/24/32/48/64/128/256）。"""
    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit("生成 EXE 图标需要 Pillow：pip install -r desktop/requirements-build.txt") from exc
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(logo_path) as image:
        image = image.convert("RGBA")
        image.save(icon_path, format="ICO",
                   sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return icon_path


def require_runtime_dependencies(packages: Iterable[str] = RUNTIME_DEPENDENCIES) -> None:
    missing = []
    for name in packages:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    if missing:
        raise SystemExit(
            "缺少构建依赖：%s\n先执行：pip install -r desktop/requirements-build.txt"
            % ", ".join(missing))


def pyinstaller_argv(*, launcher: Path, dist_dir: Path, work_dir: Path,
                     bundle_dir: Path, icon: Optional[Path] = None) -> list[str]:
    """组装 PyInstaller 命令：单文件、无窗口、webview 资源全收集。"""
    argv = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--windowed",
        "--name", EXE_NAME,
        "--hidden-import", "webview.platforms.edgechromium",
        # pywebview 自带的 hook 已收集 Windows DLL 与 JS。整包 collect-all
        # 会把 Android/GTK/Qt/macOS 渲染器也收进来；构建机若装了 PySide6，
        # 单文件包会膨胀数百 MB，且无助于 Windows WebView2。
        "--exclude-module", "webview.platforms.android",
        "--exclude-module", "webview.platforms.gtk",
        "--exclude-module", "webview.platforms.qt",
        "--exclude-module", "webview.platforms.cocoa",
        "--exclude-module", "PySide6",
        "--exclude-module", "PyQt6",
        "--exclude-module", "PyQt5",
        "--add-data", "%s%s%s" % (bundle_dir / BACKEND_ZIP_NAME, os.pathsep, BUNDLE_DIR_NAME),
        "--add-data", "%s%s%s" % (bundle_dir / PEM_NAME, os.pathsep, BUNDLE_DIR_NAME),
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        "--specpath", str(work_dir),
    ]
    for module in backend_hidden_imports():
        argv += ["--hidden-import", module]
    if icon is not None:
        argv += ["--icon", str(icon)]
    argv.append(str(launcher))
    return argv


def run_pyinstaller(argv: list[str]) -> None:
    print("运行：%s" % " ".join(argv))
    result = subprocess.run(argv, cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit("PyInstaller 失败，退出码 %d" % result.returncode)


def build_exe(*, bundle_only: bool = False, offline: bool = False) -> Path:
    launcher = DESKTOP_DIR / "launcher.py"
    if not launcher.is_file():
        raise SystemExit("缺少 %s（Windows 壳入口由同批改动提供）" % launcher)

    out_dir = OUT_DIR
    build_dir = out_dir / "build"
    bundle_dir = build_dir / "bundle" / BUNDLE_DIR_NAME
    bundle_dir.mkdir(parents=True, exist_ok=True)
    icon = write_icon(build_dir / "mytv.ico")

    result = build_backend_zip(bundle_dir / BACKEND_ZIP_NAME,
                               allow_network=not offline,
                               hls_cache=_default_hls_cache())
    print("backend.zip：%d 个条目，%d bytes" % (len(result.entries), result.zip_path.stat().st_size))
    if not PEM_SOURCE.is_file():
        raise SystemExit("缺少迁移 CA：%s" % PEM_SOURCE)
    shutil.copyfile(PEM_SOURCE, bundle_dir / PEM_NAME)
    if bundle_only:
        return bundle_dir / BACKEND_ZIP_NAME

    require_runtime_dependencies()
    argv = pyinstaller_argv(launcher=launcher, dist_dir=out_dir,
                            work_dir=build_dir, bundle_dir=bundle_dir, icon=icon)
    run_pyinstaller(argv)
    exe = out_dir / ("%s.exe" % EXE_NAME)
    if not exe.is_file():
        raise SystemExit("构建结束但没找到 %s" % exe)
    print("完成：%s（%.1f MB）" % (exe, exe.stat().st_size / 1024 / 1024))
    return exe


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-only", action="store_true",
                        help="只产出 backend.zip / 图标，不跑 PyInstaller")
    parser.add_argument("--offline", action="store_true",
                        help="不联网，复用已缓存的 hls.js")
    args = parser.parse_args()
    build_exe(bundle_only=args.bundle_only, offline=args.offline)


if __name__ == "__main__":
    main()

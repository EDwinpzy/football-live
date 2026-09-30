"""My TV Windows 桌面启动器。

职责很窄：准备后端运行环境、启动 tools/backend-src/proxy.py、把本地 WebView2 窗口指向它。
不含任何卡密门控逻辑——门控完全由后端与网页端负责，本启动器与网页版行为一致。

打包形态（PyInstaller）：
    sys._MEIPASS/desktop/backend.zip        内嵌后端（.py / json / www，不含 Linux vendor）
    sys._MEIPASS/desktop/mytv_migration_ca.pem  私有 CA 公钥
开发形态：
    直接使用仓库 tools/backend-src 源码目录。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import webview

import updater as hotupdate


APP_NAME = "MyTV"
APP_TITLE = "My TV"
PREFERRED_PORT = 8090
DEFAULT_API_ORIGIN = "https://103.236.97.137:35208"
API_ORIGIN_ENV = "OTV_DESKTOP_API_BASE_URL"
BACKEND_READY_TIMEOUT = 120.0
HEALTH_TIMEOUT = 3.0
HEALTH_RETRY_DELAY = 0.4
SELF_TEST_STARTUP_TIMEOUT = 180.0

API_ENV_KEYS = (
    "OTV_CLOUD_BASE",
    "OTV_LICENSE_ENDPOINT",
    "OTV_IPTV_LATEST_URL",
    "OTV_APK_UPDATE_OBJECT_BASE",
    "OTV_METADATA_API_URL",
    "OTV_VOD_LINE_INDEX_URL",
)

class BackendStartError(RuntimeError):
    """后端无法在本机启动。"""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """PyInstaller 解包目录（onefile 时是临时目录）。"""
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def local_app_data_root() -> Path:
    base = os.environ.get("LOCALAPPDATA", "").strip() or ""
    if not base:
        base = str(os.environ.get("USERPROFILE") or "").strip()
    if not base:
        base = str(Path.home())
    return Path(base) / APP_NAME


def ensure_std_streams() -> None:
    """PyInstaller --windowed 下 sys.stdout/stderr 可能是 None。

    proxy.main 与本文件的错误路径都直接 print()，若流为 None 会二次抛错，
    掩盖真正的启动失败。这里把它们重定向到可写日志文件。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        try:
            log_dir = local_app_data_root() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            stream = open(log_dir / "desktop.log", "a", encoding="utf-8", buffering=1)
        except OSError:
            stream = open(os.devnull, "w", encoding="utf-8")
        setattr(sys, name, stream)


def resolve_ca_file() -> Path:
    if is_frozen():
        return bundle_root() / "desktop" / "mytv_migration_ca.pem"
    return repo_root() / "android" / "app" / "src" / "main" / "res" / "raw" / "mytv_migration_ca.pem"


def resolve_backend_archive() -> Path:
    if is_frozen():
        return bundle_root() / "desktop" / "backend.zip"
    return repo_root() / "tools" / "backend-src"


def resolve_backend_dir() -> Path:
    """返回可直接 import proxy 的后端目录（开发模式即仓库源码）。"""
    if not is_frozen():
        return resolve_backend_archive()
    built_in = resolve_backend_archive()
    if not built_in.is_file():
        raise BackendStartError("frozen backend.zip is missing: %s" % built_in)
    # 热更包优先：有校验通过的 pending 目录就用它，否则回退内置包。
    source, is_update = hotupdate.resolve_backend_dir(
        hotupdate.update_root(local_app_data_root()), built_in=built_in)
    if not is_update:
        digest = _sha256_file(source)
        target = local_app_data_root() / "backend" / digest
        if not (target / ".extracted").is_file():
            extract_backend_archive(built_in, target)
        return target
    return source


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or normalized.startswith("../"):
        raise ValueError("unsafe zip member path: %r" % name)
    parts = [part for part in normalized.split("/") if part]
    if any(part == ".." for part in parts):
        raise ValueError("unsafe zip member path: %r" % name)
    # Windows 上冒号只可能来自盘符（"C:foo" 是盘相对路径，"C:\foo" 是绝对路径）。
    # 冒号在 Windows 文件名里本就非法，这里显式拒绝，不依赖 resolve() 的平台语义。
    if any(":" in part for part in parts):
        raise ValueError("unsafe zip member path: %r" % name)
    return "/".join(parts)


def extract_backend_archive(archive: Path, target: Path) -> Path:
    """把 zip 安全、完整地解到 target（先写临时目录再原子 rename）。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".extract-", dir=str(target.parent)))
    try:
        with zipfile.ZipFile(archive) as zf:
            members = zf.infolist()
            for info in members:
                name = _validate_member_name(info.filename)
                destination = (staging / name).resolve()
                if not _is_within(staging.resolve(), destination):
                    raise ValueError("unsafe zip member path: %r" % info.filename)
                mode = (info.external_attr >> 16) & 0xF000
                if mode == 0xA000:
                    raise ValueError("zip symlink member is not allowed: %r" % info.filename)
            for info in members:
                name = _validate_member_name(info.filename)
                destination = staging / name
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, open(destination, "wb") as sink:
                    shutil.copyfileobj(source, sink)
        (staging / ".extracted").write_text("ok", encoding="ascii")
        if target.exists():
            _remove_tree(target)
        os.replace(staging, target)
        staging = None
    finally:
        if staging is not None and staging.exists():
            _remove_tree(staging)
    return target


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def build_api_ssl_context(ca_path: Path) -> ssl.SSLContext:
    """装载仓库内迁移 CA，供直连自建 VPS 时校验证书。"""
    ca_text = str(ca_path or "").strip()
    if not ca_text or not os.path.isfile(ca_text):
        raise ValueError("migration CA file is missing")
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=ca_text)
    return context


def configure_api_environment(api_base_url: str, ca_path: Path) -> ssl.SSLContext:
    """按 proxy_runner.configure_api_environment 的契约配置私有 CA 与云端 origin。"""
    origin = str(api_base_url or "").strip().rstrip("/")
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("api_base_url must be an HTTPS origin") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("api_base_url must be an HTTPS origin")
    origin = "https://%s" % parsed.netloc
    context = build_api_ssl_context(ca_path)
    urllib.request.install_opener(urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context)
    ))

    os.environ["OTV_CLOUD_BASE"] = origin + "/web"
    os.environ["OTV_LICENSE_ENDPOINT"] = origin + "/activate"
    os.environ["OTV_IPTV_LATEST_URL"] = origin + "/iptv/latest"
    os.environ["OTV_APK_UPDATE_OBJECT_BASE"] = origin + "/v1/storages/object/hotupdate"
    # 私有 nginx 暴露 /metadata/ 与 /v1/storages/object/；与网页版部署保持一致。
    os.environ["OTV_METADATA_API_URL"] = origin
    os.environ["OTV_VOD_LINE_INDEX_URL"] = origin + "/v1/storages/object/hotupdate/vod-lines.json"
    return context


def _port_is_free(port: int) -> bool:
    """探测本机 loopback 端口是否空闲。

    必须用 bind() 而不是 connect_ex()：本机开着 Clash 时 connect_ex 对任意端口
    都会伪造"连上了"（见 .learnings/ERRORS.md），结果会把已占用端口误判为空闲。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def select_backend_port(preferred: int = PREFERRED_PORT) -> int:
    """优先固定端口；被占用时按固定顺序回退，保持 origin 尽量稳定。"""
    if _port_is_free(preferred):
        return preferred
    for port in range(preferred + 1, preferred + 65):
        if _port_is_free(port):
            return port
    raise BackendStartError("no free port available for the embedded backend")


def wait_for_backend(port: int, timeout: float = BACKEND_READY_TIMEOUT,
                     delay: float = HEALTH_RETRY_DELAY) -> bool:
    """健康探测 /api/health。"""
    url = "http://127.0.0.1:%d/api/health" % port
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as response:
                if response.status == 200:
                    payload = json.loads(response.read().decode("utf-8"))
                    if payload.get("ok") is True:
                        return True
        except Exception:
            pass
        time.sleep(delay)
    return False


def quickjs_smoke() -> bool:
    """确认 Windows 包内的原生 QuickJS 可执行解密脚本。"""
    try:
        import quickjs
        return quickjs.Context().eval("1 + 1") == 2
    except Exception:
        return False


def run_self_test(port: int) -> int:
    """验证健康端点、首页和 QuickJS；成功返回 0。"""
    if not wait_for_backend(port, timeout=SELF_TEST_STARTUP_TIMEOUT):
        print("self-test: backend did not become healthy on port %d" % port, file=sys.stderr)
        return 2
    index_url = "http://127.0.0.1:%d/" % port
    try:
        with urllib.request.urlopen(index_url, timeout=HEALTH_TIMEOUT) as response:
            status = response.status
            body = response.read()
    except Exception as exc:
        print("self-test: index request failed: %r" % (exc,), file=sys.stderr)
        return 3
    if status != 200 or not body.strip():
        print("self-test: unexpected index response (status=%s)" % status, file=sys.stderr)
        return 4
    if not quickjs_smoke():
        print("self-test: QuickJS runtime is unavailable", file=sys.stderr)
        return 5
    print("self-test: ok (port=%d)" % port)
    return 0


class SingleInstance:
    """基于锁文件的单实例守卫（进程内 acquire/release）。"""

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self._fd = None

    def acquire(self) -> bool:
        if sys.platform == "win32":
            return self._acquire_windows()
        return self._acquire_posix()

    def _acquire_windows(self) -> bool:
        import msvcrt

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(b"%d" % os.getpid())
            handle.flush()
        except OSError:
            pass
        self._fd = handle
        return True

    def _acquire_posix(self) -> bool:
        import fcntl

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(b"%d" % os.getpid())
            handle.flush()
        except OSError:
            pass
        self._fd = handle
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            self._fd.close()
        except OSError:
            pass
        self._fd = None


def _install_single_instance_message() -> None:
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "%s 已在运行。\n\n请使用已打开的窗口。" % APP_TITLE,
            APP_TITLE,
            0x40,  # MB_ICONINFORMATION
        )
    except Exception:
        print("%s is already running." % APP_TITLE, file=sys.stderr)


class BackendProcess:
    """在独立线程中运行 proxy.main(port)，端口只绑 loopback。"""

    def __init__(self, backend_dir: Path, port: int, data_dir: Path, web_root: Path):
        self.backend_dir = Path(backend_dir)
        self.port = int(port)
        self.data_dir = Path(data_dir)
        self.web_root = Path(web_root)
        self._thread = None
        self._error = None

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["OTV_DATA_DIR"] = str(self.data_dir)
        os.environ["OTV_WEB_ROOT"] = str(self.web_root)
        # 供网页端识别「跑在本机 EXE 里」：非空才让前端渲染版本/检查更新行。
        os.environ["OTV_DESKTOP"] = "1"
        os.environ["OTV_DESKTOP_HOT_VERSION"] = str(
            hotupdate.current_hot_version(
                hotupdate.update_root(local_app_data_root())))
        # proxy 用 cwd 写 img_cache 等运行期产物；工作目录必须是可写 data。
        os.chdir(self.data_dir)
        if str(self.backend_dir) not in sys.path:
            sys.path.insert(0, str(self.backend_dir))
        import proxy

        def _run():
            try:
                proxy.main(self.port, bind="127.0.0.1")
            except Exception as exc:  # noqa: BLE001 - 记录给上层判定启动失败
                self._error = exc
                print("backend thread failed: %r" % (exc,), file=sys.stderr)
                traceback.print_exc()

        self._thread = threading.Thread(target=_run, daemon=True, name="mytv-backend")
        self._thread.start()

    def wait_error(self, timeout: float = 1.0) -> None:
        """短暂等待线程报错，以便启动即失败时快速返回。"""
        if self._thread is None:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._thread.is_alive() or self._error is not None:
                break
            time.sleep(0.05)
        if not self._thread.is_alive() and self._error is not None:
            raise BackendStartError("backend thread failed to start: %r" % (self._error,))


def show_window(url: str, storage_path: Path, title: str = APP_TITLE) -> None:
    """显示 WebView2 窗口。

    private_mode=False + storage_path 是必须的：pywebview 默认 private_mode=True
    会让 localStorage 不落盘，卡密票据与收藏会丢。
    """
    Path(storage_path).mkdir(parents=True, exist_ok=True)
    window = webview.create_window(title, url, width=1280, height=800, min_size=(960, 600))
    webview.start(
        gui="edgechromium",
        private_mode=False,
        storage_path=str(storage_path),
    )


def _show_error_dialog(message: str, detail: str = "") -> None:
    text = message if not detail else "%s\n\n%s" % (message, detail)
    try:
        ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x10)  # MB_ICONERROR
    except Exception:
        print(text, file=sys.stderr)


def _api_origin() -> str:
    return os.environ.get(API_ORIGIN_ENV, "").strip() or DEFAULT_API_ORIGIN


def start_background_update(api_origin: str,
                            context: ssl.SSLContext) -> None:
    """窗口起来之后在后台检查/下载热更包，失败一律静默。"""
    if not is_frozen():
        return  # 开发模式直接用仓库源码，没有"更新"概念
    try:
        hot_version = hotupdate.current_hot_version(
            hotupdate.update_root(local_app_data_root()))
        service = hotupdate.BackgroundUpdater(
            app_data_root=local_app_data_root(),
            api_origin=api_origin,
            # 服务端要求 64 位十六进制设备标识（与卡密同一套校验），
            # 本地持久化，保证同一台机器多次启动是同一个 id。
            device_id=hotupdate.device_id(local_app_data_root()),
            hot_version=hot_version,
            apk_version=0,
            context=context,
        )

        def _log(stage: str, detail: str = "") -> None:
            print("updater: %s %s" % (stage, detail), file=sys.stderr)

        service.on_state = _log
        service.start()
    except Exception as exc:  # noqa: BLE001 - 更新绝不影响启动
        print("updater init failed: %r" % (exc,), file=sys.stderr)


def main(argv=None) -> int:
    ensure_std_streams()
    parser = argparse.ArgumentParser(description="My TV desktop launcher")
    parser.add_argument("--self-test", action="store_true",
                        help="start the backend, verify health and index, then exit")
    args = parser.parse_args(argv)

    try:
        backend_dir = resolve_backend_dir()
        data_dir = local_app_data_root() / "data"
        web_root = (backend_dir / "www")
        if not (web_root / "app.js").is_file():
            raise BackendStartError("web assets missing: %s" % (web_root / "app.js"))

        api_context = configure_api_environment(_api_origin(), resolve_ca_file())
        port = select_backend_port()

        instance = SingleInstance(local_app_data_root() / "instance.lock")
        if not instance.acquire():
            _install_single_instance_message()
            return 0
        try:
            backend = BackendProcess(backend_dir, port, data_dir, web_root)
            backend.start()
            backend.wait_error()

            if args.self_test:
                code = run_self_test(port)
                return code

            if not wait_for_backend(port):
                raise BackendStartError("backend did not become healthy on port %d" % port)

            start_background_update(_api_origin(), api_context)
            storage_path = local_app_data_root() / "webview-profile"
            show_window("http://127.0.0.1:%d/" % port, storage_path)
            return 0
        finally:
            instance.release()
    except BackendStartError as exc:
        _show_error_dialog("My TV 启动失败", str(exc))
        print("launcher error: %s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，避免静默退出
        _show_error_dialog("My TV 启动失败", "%r\n\n%s" % (exc, traceback.format_exc(limit=6)))
        print("launcher unexpected error: %r" % (exc,), file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""重建内置后端 zip：以 app assets 里现有的 python-backend.zip 为基础，
用 tools/backend-src/ 下的源文件覆盖同名条目，输出写回 assets/backend/python-backend.zip。

2026-09-07 需求⑤（热更含前后端）：REPL 从 5 个后端文件扩到全量——
www/index.html、www/app.js、www/style.css、www/manifest.webmanifest、www/assets/*
（logo 等前端静态资源）一并随包更新。内嵌后端托管 www（127.0.0.1:8090），
此前 www 改动不进热更包 = 网页热更发不下去，只能发 APK。

2026-08-29 需求 足球海报池更换：删除全部旧海报 www/assets/football/fb-*.webp，
写入 D:/OneDrive/ZCode/football-posters 下的新海报（统一重命名 fb-01.jpg…，按原文件名排序），
pool 大小由 LiveRepository.posterUrl 同步维护（POSTER_POOL = 36）。

用法: python build_backend_zip.py
"""
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

try:
    from .build_web_assets import build_assets
except ImportError:  # direct `python tools/build_backend_zip.py` invocation
    from build_web_assets import build_assets

ROOT = str(Path(__file__).resolve().parent.parent)
SRC_ZIP = os.path.join(ROOT, "android", "app", "src", "main", "assets", "backend", "python-backend.zip")
MOBILE_ZIP = os.path.join(ROOT, "internal", "mobile", "app", "src", "main", "assets", "backend", "python-backend.zip")
TMP_ZIP = os.path.join(ROOT, "tools", "python-backend-new.zip")
SRC_DIR = os.path.join(ROOT, "tools", "backend-src")
POSTER_DIR = r"D:\OneDrive\ZCode\football-posters"

REPL = ["proxy.py", "vlc_hls.py", "hhkan.py", "hhkan_snapshot.py", "hhkan_snapshot.json",
        "team_backdrop.py", "scraper.py", "decrypt_stream.js",
        "douban_catalog.py", "douban_snapshot.json", "douban_filter_matrix.json",
        "media_index.py", "pinyin_map.json", "ime_hotwords.py", "vod_api.py",
        "vod_sources.py", "vod_sources.default.json",
        # 下面三个都是顶层 import，曾因白名单手工维护而漏收，打出来的 zip
        # 在真机启动时直接 ModuleNotFoundError，整个内嵌 Python 后端起不来，
        # 影视详情 / 播放 / 片源全部失效：
        #   proxy.py    import metadata_client / import vod_resolver
        #   hhkan.py    import source_dns
        # vod_resolver 是方案 §5/§6 的片源解析器。延迟导入的 decrypt_python
        # 有 quickjs 兜底、不属于打包必需，不在此列。
        # tests/test_backend_zip_completeness.py 用 AST 静态分析顶层 import，
        # 新增硬依赖必须同步登记到这里。
        "source_dns.py", "vod_resolver.py", "metadata_client.py"]
# 需求⑤：热更包纳入前端（www 根文件 + www/assets 下全部前端资源；football 海报池仍走整池替换）
WWW_FILES = ["index.html", "app.js", "style.css", "manifest.webmanifest"]
WWW_ASSETS_PREFIX = "www/assets/"
FB_PREFIX = "www/assets/football/"
# ZIP 条目时间统一固定，确保相同源码重复构建得到相同 SHA-256。
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
_HASHED_WEB_REFERENCE_RE = re.compile(
    r"^assets/(?:app\.[0-9a-f]{16}\.js|style\.[0-9a-f]{16}\.css)$"
)


def write_entry(zout, filename, data, template=None):
    info = zipfile.ZipInfo(filename, FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    if template is not None:
        info.create_system = template.create_system
        info.external_attr = template.external_attr
        info.internal_attr = template.internal_attr
        info.comment = template.comment
    else:
        info.create_system = 3
        info.external_attr = 0o644 << 16
    zout.writestr(info, data)


def atomic_copy_file(source, destination):
    """Replace a generated archive without truncating the current package in place."""
    destination = Path(destination)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(
                prefix=destination.name + ".", suffix=".tmp",
                dir=str(destination.parent), delete=False) as output:
            staged = output.name
            with open(source, "rb") as input_file:
                shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staged, str(destination))
        staged = None
    finally:
        if staged and os.path.exists(staged):
            os.remove(staged)


def copied_web_files(manifest_bytes):
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return set()
    files = manifest.get("copiedFiles", []) if isinstance(manifest, dict) else []
    if not isinstance(files, list):
        return set()
    return {"www/" + name for name in files if isinstance(name, str)}


def referenced_web_files(manifest_bytes):
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return set()
    files = manifest.get("referencedFiles", []) if isinstance(manifest, dict) else []
    if not isinstance(files, list):
        return set()
    return {
        name for name in files
        if isinstance(name, str) and _HASHED_WEB_REFERENCE_RE.fullmatch(name)
    }


def main():
    for name in REPL:
        src = os.path.join(SRC_DIR, name)
        if not os.path.isfile(src):
            raise SystemExit("缺少 %s" % src)
    for name in WWW_FILES:
        src = os.path.join(SRC_DIR, "www", name)
        if not os.path.isfile(src):
            raise SystemExit("缺少 %s" % src)
    with tempfile.TemporaryDirectory(prefix="mytv-web-assets-") as build_temp:
        web_dist = Path(build_temp) / "www"
        build_assets(Path(SRC_DIR) / "www", web_dist)
        web_files = {}
        for full in sorted(path for path in web_dist.rglob("*") if path.is_file()):
            rel = ("www/" + full.relative_to(web_dist).as_posix())
            if rel.startswith(FB_PREFIX) or rel.endswith(".log"):
                continue
            web_files[rel] = full.read_bytes()
    # Unversioned source files remain available to tools and old local links;
    # the generated index itself references only the content-hashed bundle.
    web_files["www/app.js"] = Path(SRC_DIR, "www", "app.js").read_bytes()
    web_files["www/style.css"] = Path(SRC_DIR, "www", "style.css").read_bytes()
    asset_files = [name for name in web_files if name.startswith("www/assets/")]
    posters = sorted(
        (f for f in os.listdir(POSTER_DIR)
         if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) and not f.startswith(".")),
    ) if os.path.isdir(POSTER_DIR) else []
    replace_posters = bool(posters)
    print("%s；www 前端文件 %d + assets %d" % (
        ("新海报 %d 张" % len(posters)) if replace_posters else "未找到外部海报目录，保留包内现有海报",
        len(WWW_FILES), len(asset_files)))

    if os.path.exists(TMP_ZIP):
        os.remove(TMP_ZIP)
    zin = zipfile.ZipFile(SRC_ZIP, "r")
    zout = zipfile.ZipFile(TMP_ZIP, "w", zipfile.ZIP_DEFLATED)
    replaced, dropped, added = [], 0, 0
    # 热更包内的 HTML 引用按内容哈希命名的 JS/CSS；其余静态资源仍按原路径复制。
    previous_copies = set()
    previous_refs = set()
    existing_names = set(zin.namelist())
    try:
        previous_manifest_bytes = zin.read("www/manifest.json")
        previous_copies = copied_web_files(previous_manifest_bytes)
        previous_refs = referenced_web_files(previous_manifest_bytes)
    except KeyError:
        pass
    current_manifest_bytes = web_files.get("www/manifest.json", b"{}")
    current_copies = copied_web_files(current_manifest_bytes)
    current_refs = referenced_web_files(current_manifest_bytes)
    for relative in previous_refs - current_refs:
        archive_path = "www/" + relative
        if archive_path in existing_names:
            web_files[archive_path] = zin.read(archive_path)
    www_replace = set(web_files)
    stale_copies = previous_copies - current_copies - {"www/" + path for path in previous_refs}
    stale_web_assets = 0
    for item in zin.infolist():
        base = os.path.basename(item.filename)
        if item.filename in REPL or (base in REPL and "/" not in item.filename):
            data = open(os.path.join(SRC_DIR, base), "rb").read()
            write_entry(zout, item.filename, data, item)
            replaced.append(item.filename)
        elif replace_posters and item.filename.startswith(FB_PREFIX):
            dropped += 1  # 旧海报池整池移除（需求：原来的海报都去掉）
        elif (item.filename == "www/asset-manifest.json"
              or (item.filename in stale_copies and not item.filename.startswith(FB_PREFIX))):
            stale_web_assets += 1  # 移除旧清单记录的静态文件及旧版清单
        elif (item.filename.startswith(WWW_ASSETS_PREFIX)
              and not item.filename.startswith(FB_PREFIX)
              and not item.filename.endswith(".log")
              and item.filename not in www_replace):
            stale_web_assets += 1  # 同步删除已从普通静态目录移除的文件
        elif item.filename in www_replace:
            data = web_files[item.filename]
            write_entry(zout, item.filename, data, item)
            replaced.append(item.filename)
        else:
            write_entry(zout, item.filename, zin.read(item.filename), item)
    # www_replace 中 zip 里没有的（新文件）也写入
    existing = existing_names
    # 新增的后端模块/数据文件（例如好好看快照）在旧 zip 中没有同名条目，
    # 必须显式写入；旧实现只会覆盖既有 REPL 文件。
    for name in REPL:
        if name not in existing:
            write_entry(zout, name, open(os.path.join(SRC_DIR, name), "rb").read())
            added += 1
    for rel in sorted(www_replace):
        if rel not in existing:
            write_entry(zout, rel, web_files[rel])
            added += 1
    for i, f in enumerate(posters):
        ext = ".jpg" if f.lower().endswith((".jpg", ".jpeg")) else os.path.splitext(f)[1].lower()
        with open(os.path.join(POSTER_DIR, f), "rb") as fh:
            write_entry(zout, "%sfb-%02d%s" % (FB_PREFIX, i + 1, ext), fh.read())
        added += 1
    zout.close()
    zin.close()
    atomic_copy_file(TMP_ZIP, SRC_ZIP)
    atomic_copy_file(TMP_ZIP, MOBILE_ZIP)
    os.remove(TMP_ZIP)
    print("已替换:", replaced[:12], ("…共 %d 项" % len(replaced)) if len(replaced) > 12 else "")
    print("新增 %d 项；旧海报删除 %d 个，过期 Web 哈希资源删除 %d 个，新海报写入 %d 个" % (
        added, dropped, stale_web_assets, len(posters)))
    print("zip 大小:", os.path.getsize(SRC_ZIP))


if __name__ == "__main__":
    main()

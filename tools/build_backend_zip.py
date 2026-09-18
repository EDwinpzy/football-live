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
import os
import shutil
import zipfile
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
SRC_ZIP = os.path.join(ROOT, r"android\app\src\main\assets\backend\python-backend.zip")
MOBILE_ZIP = os.path.join(ROOT, r"internal\mobile\app\src\main\assets\backend\python-backend.zip")
TMP_ZIP = os.path.join(ROOT, r"tools\python-backend-new.zip")
SRC_DIR = os.path.join(ROOT, r"tools\backend-src")
POSTER_DIR = r"D:\OneDrive\ZCode\football-posters"

REPL = ["proxy.py", "hhkan.py", "hhkan_snapshot.py", "hhkan_snapshot.json",
        "team_backdrop.py", "scraper.py", "decrypt_stream.js",
        "douban_catalog.py", "douban_snapshot.json", "douban_filter_matrix.json",
        "media_index.py", "pinyin_map.json", "vod_api.py",
        "vod_sources.py", "vod_sources.default.json"]
# 需求⑤：热更包纳入前端（www 根文件 + www/assets 下全部前端资源；football 海报池仍走整池替换）
WWW_FILES = ["index.html", "app.js", "style.css", "manifest.webmanifest"]
WWW_ASSETS_PREFIX = "www/assets/"
FB_PREFIX = "www/assets/football/"
# ZIP 条目时间统一固定，确保相同源码重复构建得到相同 SHA-256。
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


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


def main():
    for name in REPL:
        src = os.path.join(SRC_DIR, name)
        if not os.path.isfile(src):
            raise SystemExit("缺少 %s" % src)
    for name in WWW_FILES:
        src = os.path.join(SRC_DIR, "www", name)
        if not os.path.isfile(src):
            raise SystemExit("缺少 %s" % src)
    # www/assets 下前端资源（logo 等；football 海报池单独整池替换，此处跳过）
    asset_files = []
    for base, _dirs, files in os.walk(os.path.join(SRC_DIR, "www", "assets")):
        for f in files:
            full = os.path.join(base, f)
            rel = ("www/" + os.path.relpath(full, os.path.join(SRC_DIR, "www"))).replace("\\", "/")
            if rel.startswith(FB_PREFIX) or rel.endswith(".log"):
                continue
            asset_files.append(rel)
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
    # 需求⑤：热更目标集合 = 后端五件套 + www 根文件 + www/assets/**（football 除外）
    www_replace = set("www/" + n for n in WWW_FILES) | set(asset_files)
    for item in zin.infolist():
        base = os.path.basename(item.filename)
        if item.filename in REPL or (base in REPL and "/" not in item.filename):
            data = open(os.path.join(SRC_DIR, base), "rb").read()
            write_entry(zout, item.filename, data, item)
            replaced.append(item.filename)
        elif replace_posters and item.filename.startswith(FB_PREFIX):
            dropped += 1  # 旧海报池整池移除（需求：原来的海报都去掉）
        elif item.filename in www_replace:
            data = open(os.path.join(SRC_DIR, item.filename), "rb").read()
            write_entry(zout, item.filename, data, item)
            replaced.append(item.filename)
        else:
            write_entry(zout, item.filename, zin.read(item.filename), item)
    # www_replace 中 zip 里没有的（新文件）也写入
    with zipfile.ZipFile(SRC_ZIP, "r") as zexist:
        existing = set(zexist.namelist())
    # 新增的后端模块/数据文件（例如好好看快照）在旧 zip 中没有同名条目，
    # 必须显式写入；旧实现只会覆盖既有 REPL 文件。
    for name in REPL:
        if name not in existing:
            write_entry(zout, name, open(os.path.join(SRC_DIR, name), "rb").read())
            added += 1
    for rel in sorted(www_replace):
        if rel not in existing:
            write_entry(zout, rel, open(os.path.join(SRC_DIR, rel), "rb").read())
            added += 1
    for i, f in enumerate(posters):
        ext = ".jpg" if f.lower().endswith((".jpg", ".jpeg")) else os.path.splitext(f)[1].lower()
        with open(os.path.join(POSTER_DIR, f), "rb") as fh:
            write_entry(zout, "%sfb-%02d%s" % (FB_PREFIX, i + 1, ext), fh.read())
        added += 1
    zout.close()
    zin.close()
    shutil.copyfile(TMP_ZIP, SRC_ZIP)
    shutil.copyfile(TMP_ZIP, MOBILE_ZIP)
    os.remove(TMP_ZIP)
    print("已替换:", replaced[:12], ("…共 %d 项" % len(replaced)) if len(replaced) > 12 else "")
    print("新增 %d 项；旧海报删除 %d 个，新海报写入 %d 个" % (added, dropped, len(posters)))
    print("zip 大小:", os.path.getsize(SRC_ZIP))


if __name__ == "__main__":
    main()

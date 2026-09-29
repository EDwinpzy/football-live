#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨版本共享代码同步检查/同步工具（内测/公测 × TV/移动 四版本共用）。

背景：四个版本目录（内测版 TV/移动、公测版 TV/移动）各自独立成工程，
UI 层（ui/、MainActivity）按端差异化，但数据层/播放层/内置后端必须保持一致
（用户要求：有修改所有版本要同步修改）。本工具按「共享清单」比对哈希，
报出漂移；也可把某版本的共享文件单向覆盖到另一版本。

用法：
  python tools/sync_shared.py                     # 只检查，输出漂移报告
  python tools/sync_shared.py --source internal/mobile --target android
                                                  # 把移动版的共享文件同步到内测 TV 版
  python tools/sync_shared.py --list              # 列出发现的版本目录

版本目录自动发现（存在 app/build.gradle 即认为是 Android 工程根）：
  android            内测版 TV（整理前的位置，最终会移到 internal/tv）
  internal/tv        内测版 TV（整理后的位置）
  internal/mobile    内测版 移动版
  public/tv          公测版 TV（另一会话交付后放入）
  public/mobile      公测版 移动版（等公测 TV 定稿后适配）
"""
import argparse
import hashlib
import shutil
import sys
from pathlib import Path

# Windows 终端仍可能以 GBK 打开 stdout；检查成功时的 Unicode 状态符不应反过来让
# 工具以异常退出，导致 CI/发布脚本把「全绿」误判为失败。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

ROOT = Path(__file__).resolve().parent.parent

# 版本目录候选（相对项目根）；整理阶段 android/ 与 internal/tv 只会存在其一
VERSION_DIRS = ["android", "internal/tv", "internal/mobile", "public/tv", "public/mobile"]

# 共享清单：这些路径在所有版本必须字节级一致（存在即参与比对；目录递归）
# 差异允许清单（明确不参与字节级同步的文件，写明原因）
# 注意：TV 版独有 cpp/（拼音解码器）、Focus.kt/PinyinIme.kt/TabBar.kt；移动版独有 MobileChrome.kt 等——
# 这些都在 ui/ 或 cpp/ 下，本就不在共享清单里，无需列出。
# 2026-09-11 需求⑭追加：TV 版投屏全删——PlayerViewModel 从共享清单移入差异允许
# （TV 已剥离 v1.21 DLNA 全部状态/方法并删除 data/cast/DlnaCast.kt；移动版完整保留
# DLNA 投屏。此后两端的 PlayerViewModel 演进需各自手动同步非投屏部分）。
SHARED_PATHS = [
    "app/src/main/java/com/qiubo/optimaltv/App.kt",
    "app/src/main/java/com/qiubo/optimaltv/EmbeddedBackend.kt",
    "app/src/main/java/com/qiubo/optimaltv/OtvLog.kt",
    "app/src/main/java/com/qiubo/optimaltv/data",            # 数据层（repo/source/model/db/prefs；TV 无 data/cast）
    "app/src/main/java/com/qiubo/optimaltv/playback",        # 播放引擎层（Media3/VLC/解密桥）
    "app/src/main/java/com/qiubo/optimaltv/ui/detail/DetailViewModel.kt",  # 详情 VM（双端共用）
    "app/src/main/java/com/qiubo/optimaltv/license",           # v1.17 卡密授权管理（双端字节级一致）
    "app/src/main/java/com/qiubo/optimaltv/apkupdate",         # APK 更新协议/下载校验（双端字节级一致）
    "app/src/main/java/com/qiubo/optimaltv/ui/paywall/PaywallShared.kt",  # v1.17 会员共享层（套餐/二维码/门控；PaywallScreen/ActivateScreen 各端独立）
    "app/src/main/python",                                   # Chaquopy 后端入口（proxy_runner 等）
    "app/src/main/assets/backend",                           # 内置后端 zip（proxy.py 等）
    "app/src/main/res/xml/file_paths.xml",                   # FileProvider 仅暴露 APK 更新目录
    # 品牌资产不共享：TV 线 2026-09-02 更名 My TV（my_tv_logo），移动版按用户要求
    # 保持交付原貌（apple_tv_logo）——两端口径分治，不进字节级同步清单
]

# 已知差异允许（存在即合理，检查模式跳过漂移比对；改动需在此登记原因）
ALLOW_DRIFT = {
    # 2026-09-18 需求：移动端会员页去掉二维码、改按钮续费，RenewalGateway 只进移动端；
    # TV 版无此通道（当时漏登记，2026-09-22 补录，避免同步工具误覆盖移动端功能）。
    "app/src/main/java/com/qiubo/optimaltv/ui/paywall/PaywallShared.kt":
        "移动端独有 RenewalGateway（按钮续费），TV 版无此通道",
    # 2026-09-11 需求⑭：TV 版移除 DLNA 投屏（用户明确只删 TV，移动版保留）；
    # v1.23 时 TV 已删 UI 入口，本轮连共享 VM/data 层一并剥离。
    "app/src/main/java/com/qiubo/optimaltv/ui/player/PlayerViewModel.kt": "TV 剥离投屏（2026-09-11），移动版保留 DLNA",
}


def discover_versions():
    """返回存在的版本目录列表 [(名字, Path)]。"""
    found = []
    for d in VERSION_DIRS:
        p = ROOT / d
        if (p / "app" / "build.gradle").exists():
            found.append((d.replace("/", "-"), p))
    return found


def iter_shared_files(proj: Path):
    """产出工程内所有共享文件的相对路径（相对项目根，含 Posix 分隔）。
    排除 .mimosa 工具元数据目录（不是源码）与 __pycache__（字节码缓存，
    内嵌绝对路径不同必然哈希不同，是纯噪声，会淹没真实漂移）。"""
    for rel in SHARED_PATHS:
        p = proj / rel
        if p.is_file():
            yield rel.replace("\\", "/")
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and ".mimosa" not in f.parts and "__pycache__" not in f.parts:
                    yield str(f.relative_to(proj)).replace("\\", "/")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="四版本共享代码同步检查")
    ap.add_argument("--source", help="同步源版本目录（如 internal/mobile）")
    ap.add_argument("--target", help="同步目标版本目录（如 android）")
    ap.add_argument("--list", action="store_true", help="只列出版本目录")
    args = ap.parse_args()

    versions = discover_versions()
    if args.list:
        for name, p in versions:
            print(f"{name:20s} {p}")
        return

    if not versions:
        print("未发现任何版本工程目录（需存在 app/build.gradle）")
        sys.exit(1)

    print("发现的版本：")
    for name, p in versions:
        print(f"  {name:20s} {p}")
    print()

    # 收集各版本共享文件哈希
    table = {}   # rel_path -> {version: hash}
    for name, proj in versions:
        for rel in iter_shared_files(proj):
            table.setdefault(rel, {})[name] = sha256(proj / rel)

    drift, missing = [], []
    for rel in sorted(table):
        hashes = table[rel]
        uniq = set(hashes.values())
        if len(hashes) < len(versions):
            have = set(hashes)
            lack = [n for n, _ in versions if n not in have]
            missing.append((rel, lack))
        if len(uniq) > 1 and rel not in ALLOW_DRIFT:
            drift.append((rel, hashes))

    # ---- 同步模式 ----
    if args.source and args.target:
        src = ROOT / args.source
        dst = ROOT / args.target
        if not (src / "app" / "build.gradle").exists() or not (dst / "app" / "build.gradle").exists():
            print("--source/--target 必须都是版本工程目录")
            sys.exit(1)
        copied = 0
        for rel in iter_shared_files(src):
            s, d = src / rel, dst / rel
            if not d.exists() or sha256(s) != sha256(d):
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)
                print(f"同步 {rel}")
                copied += 1
        print(f"\n完成：{args.source} → {args.target}，复制/覆盖 {copied} 个共享文件。")
        print("注意：assets/backend 变更后需要各版本重新构建 APK 才会生效。")
        return

    # ---- 检查模式 ----
    if drift:
        print(f"⚠ 漂移（{len(drift)} 个共享文件版本间不一致）：")
        for rel, hashes in drift:
            print(f"  {rel}")
            for name, h in hashes.items():
                print(f"    {name:20s} {h[:12]}")
    else:
        print("✓ 所有共享文件在已存在的版本间保持一致。")

    if missing:
        print(f"\nℹ 部分版本缺失以下共享文件（可能是 TV/移动端合理差异，核对后补齐）：")
        for rel, lack in missing:
            print(f"  {rel}  缺于: {', '.join(lack)}")

    sys.exit(1 if drift else 0)


if __name__ == "__main__":
    main()

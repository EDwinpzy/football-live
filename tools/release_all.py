#!/usr/bin/env python3
"""构建本地前后端交付物，并按显式参数执行网页版部署。

改动 tools/backend-src/（后端五件套 + www/ 前端）后默认只做本地构建：
  1. build_web_function.py  汇组云端 otv-web 部署包
  2. build_backend_zip.py    重建 App 内嵌 python-backend.zip（www 前端随包）

得到用户明确确认后，才使用 `--deploy` 部署云端网页版；热更新包上传/发布
仍必须在后台单独操作，不由本脚本自动推送。

用法: python tools/release_all.py
部署: python tools/release_all.py --deploy
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
CLOUDBASE_ENV_ID = "appletv-d5ge1bth794873f76"


def cloudbase_code_update_command(tcb):
    """Update otv-web code without redeploying its existing function config."""
    return [
        tcb,
        "fn",
        "code",
        "update",
        "otv-web",
        "--dir",
        ".",
        "-e",
        CLOUDBASE_ENV_ID,
        "--json",
    ]


def run(cmd, cwd, input_text=None):
    print("$", " ".join(cmd) if isinstance(cmd, list) else cmd, f"(cwd={cwd})")
    # 让子进程直接继承控制台，避免 Windows 上 GBK/UTF-8 混合输出在捕获解码时崩溃。
    r = subprocess.run(cmd, cwd=cwd, input=input_text, text=True, shell=False)
    return r.returncode == 0


def main():
    parser = argparse.ArgumentParser(description="Build local deliverables; deploy only with explicit --deploy")
    parser.add_argument("--deploy", action="store_true",
                        help="update otv-web code while preserving its existing function configuration")
    args = parser.parse_args()

    # 豆瓣快照是云端/离线环境唯一的影视数据来源（首页、详情、本地搜索都靠它）：
    # 发布前刷新（快照不足 24 小时则自动跳过，避免每次发布都等抓取）。
    # 抓取被限流时脚本自己会保留旧快照并返回非零码，这里只提示不阻塞。
    if not run([PY, os.path.join(ROOT, "tools", "build_douban_snapshot.py")], ROOT):
        print("[WARN] 豆瓣快照未刷新，沿用现有 douban_snapshot.json 继续构建")

    # 筛选能力矩阵：豆瓣 Explore 对筛选参数的支持按分类而异，这个矩阵决定某个维度交给
    # 上游筛还是本地兜底判。矩阵是数据文件，重跑本脚本即可随包更新，不用改代码。
    # 探测要能连通豆瓣，失败不阻塞构建（沿用现有 douban_filter_matrix.json）。
    if not run([PY, os.path.join(ROOT, "tools", "probe_douban_filter_matrix.py")], ROOT):
        print("[WARN] 筛选能力矩阵未刷新，沿用现有 douban_filter_matrix.json 继续构建")

    # 首装离线目录种子（assets/catalog_seed.json）由快照派生：字段口径必须与
    # VodRepository.decodeSnapshot 一致（v=2 / 仅豆瓣卡片 / 海报走内置后端中继）。
    # 快照一刷新就必须重生成——2026-09-18 前的种子还是退役的好好看格式，
    # 首装那一瞬没有目录可上屏（日志「内置种子 已损坏或含非好好看卡片，跳过」）。
    if not run([PY, os.path.join(ROOT, "tools", "build_catalog_seed.py")], ROOT):
        raise SystemExit("build_catalog_seed 失败（首装离线目录种子会缺失或过期）")

    ok1 = run([PY, os.path.join(ROOT, "tools", "build_web_function.py")], ROOT)
    if not ok1:
        raise SystemExit("build_web_function 失败")

    # 先完成全部本地交付物构建，再考虑任何外部动作。
    ok3 = run([PY, os.path.join(ROOT, "tools", "build_backend_zip.py")], ROOT)
    if not ok3:
        raise SystemExit("build_backend_zip 失败")

    if args.deploy:
        tcb = shutil.which("tcb.cmd") or shutil.which("tcb")
        if not tcb:
            raise SystemExit("未找到 tcb CLI；请先确认 npm 全局 bin 已加入 PATH")
        ok2 = run(
            cloudbase_code_update_command(tcb),
            os.path.join(ROOT, "cloudfunctions", "otv-web"),
        )
        if not ok2:
            raise SystemExit("tcb 更新 otv-web 代码失败（函数环境变量、运行时和触发器未由本命令修改）")
    if args.deploy:
        print("\n[OK] 本地交付物已构建\n[OK] CloudBase otv-web 代码已更新（保留现有函数配置）\n"
              "[OK] App 热更包已重建（android/app/src/main/assets/backend/python-backend.zip，含 www 前端）\n"
              "-> App 热更新仍需到后台手动上传并发布。")
    else:
        print("\n[OK] 本地交付物已构建\n"
              "[OK] 云端网页版部署包已生成（未部署）\n"
              "[OK] App 热更包已重建（android/app/src/main/assets/backend/python-backend.zip，含 www 前端）\n"
              "[PAUSED] 未执行部署、上传或推送；获得用户明确确认后再运行 --deploy，并在后台发布热更新。")


if __name__ == "__main__":
    main()

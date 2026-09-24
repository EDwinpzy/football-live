#!/usr/bin/env python3
"""Build the local web/backend artifacts and optionally deploy the self-hosted stack."""

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def run(cmd, cwd, input_text=None):
    print("$", " ".join(cmd) if isinstance(cmd, list) else cmd, f"(cwd={cwd})")
    # Let subprocesses inherit the console to avoid mixed Windows console encodings.
    result = subprocess.run(cmd, cwd=cwd, input=input_text, text=True, shell=False)
    return result.returncode == 0


def selfhosted_deploy_command(root=ROOT):
    deploy_dir = os.path.join(root, "deploy", "selfhosted")
    compose_file = os.path.join(deploy_dir, "compose.yaml")
    return [
        "docker",
        "compose",
        "--project-directory",
        deploy_dir,
        "-f",
        compose_file,
        "up",
        "-d",
        "--build",
    ]


def main():
    parser = argparse.ArgumentParser(description="Build local deliverables; deploy with --deploy")
    parser.add_argument("--deploy", action="store_true",
                        help="run the configured self-hosted Docker Compose deployment entry")
    args = parser.parse_args()

    # Refresh source data when possible; a transient upstream failure preserves the
    # checked-in snapshot and does not block a local build.
    if not run([PY, os.path.join(ROOT, "tools", "build_douban_snapshot.py")], ROOT):
        print("[WARN] 豆瓣快照未刷新，沿用现有 douban_snapshot.json 继续构建")
    if not run([PY, os.path.join(ROOT, "tools", "probe_douban_filter_matrix.py")], ROOT):
        print("[WARN] 筛选能力矩阵未刷新，沿用现有 douban_filter_matrix.json 继续构建")
    if not run([PY, os.path.join(ROOT, "tools", "build_catalog_seed.py")], ROOT):
        raise SystemExit("build_catalog_seed 失败（首装离线目录种子会缺失或过期）")

    # The Web task owns this static asset builder and its no-argument interface.
    if not run([PY, os.path.join(ROOT, "tools", "build_web_assets.py")], ROOT):
        raise SystemExit("build_web_assets 失败")
    if not run([PY, os.path.join(ROOT, "tools", "build_backend_zip.py")], ROOT):
        raise SystemExit("build_backend_zip 失败")

    if args.deploy:
        deploy_dir = os.path.join(ROOT, "deploy", "selfhosted")
        if not run(selfhosted_deploy_command(ROOT), deploy_dir):
            raise SystemExit("self-hosted Docker Compose deployment failed")
        print("\n[OK] 本地 Web 静态资源与 App 后端包已构建\n"
              "[OK] 已调用自建 Docker Compose 部署入口")
    else:
        print("\n[OK] 本地 Web 静态资源与 App 后端包已构建\n"
              "[PAUSED] 未执行部署")


if __name__ == "__main__":
    main()

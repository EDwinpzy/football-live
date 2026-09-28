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


def script_path(root, *parts):
    return os.path.join(root, "tools", *parts)


def required_scripts(root=ROOT):
    """发布器会用到的仓库内脚本；缺任何一个都要在启动前说清楚。"""
    return [
        script_path(root, "build_douban_snapshot.py"),
        script_path(root, "probe_douban_filter_matrix.py"),
        script_path(root, "build_catalog_seed.py"),
        script_path(root, "build_web_assets.py"),
        script_path(root, "build_backend_zip.py"),
    ]


def required_deploy_files(root=ROOT):
    """自建部署入口依赖的编排文件与一次性迁移脚本。"""
    return [
        os.path.join(root, "deploy", "selfhosted", "compose.yaml"),
        os.path.join(root, "tools", "selfhosted", "apply_schema_migrations.sh"),
    ]


def missing_files(paths):
    return [path for path in paths if not os.path.isfile(path)]


def require_files(paths, label):
    missing = missing_files(paths)
    if missing:
        # os.path.relpath raises ValueError when the paths sit on different
        # Windows drives, which would replace the actionable message with a
        # traceback. Fall back to the absolute path in that case.
        def display(path):
            try:
                return os.path.relpath(path, ROOT)
            except ValueError:
                return path

        listed = "\n".join(f"  - {display(path)}" for path in missing)
        raise SystemExit(f"{label} 缺少必需文件，请先修复仓库后再发布：\n{listed}")


def selfhosted_compose_base(root=ROOT):
    deploy_dir = os.path.join(root, "deploy", "selfhosted")
    compose_file = os.path.join(deploy_dir, "compose.yaml")
    return [
        "docker",
        "compose",
        "--project-directory",
        deploy_dir,
        "-f",
        compose_file,
    ]


def selfhosted_migration_command(root=ROOT):
    """一次性迁移容器必须显式重跑：bind mount 的 SQL/密码变化不会触发 Compose 重建。"""
    return selfhosted_compose_base(root) + [
        "run",
        "--rm",
        "schema-migrations",
    ]


def selfhosted_deploy_command(root=ROOT):
    return [
        *selfhosted_compose_base(root),
        "up",
        "-d",
        "--build",
    ]


def selfhosted_nginx_reload_command(root=ROOT):
    """nginx.conf 是 bind mount，改它不会让 nginx 进程重读配置。

    与 schema-migrations 同源的问题：容器一直活着，配置已经变了但旧配置
    仍在生效。实际踩过一次 —— /admin/ 路由已写进 nginx.conf，线上却仍然
    404，因为 nginx 从 2026-09-23 起就没 reload 过。
    """
    return selfhosted_compose_base(root) + [
        "exec",
        "-T",
        "nginx",
        "nginx",
        "-s",
        "reload",
    ]


def main():
    parser = argparse.ArgumentParser(description="Build local deliverables; deploy with --deploy")
    parser.add_argument("--deploy", action="store_true",
                        help="run the configured self-hosted Docker Compose deployment entry")
    args = parser.parse_args()

    require_files(required_scripts(ROOT), "发布器")
    if args.deploy:
        require_files(required_deploy_files(ROOT), "自建部署入口")

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
        # 迁移先跑：schema-migrations 是一次性容器，restart="no"，
        # 后续 `up` 不会因为 SQL 变化重跑它，失败时必须挡住 API 启动。
        if not run(selfhosted_migration_command(ROOT), deploy_dir):
            raise SystemExit(
                "schema-migrations 一次性迁移失败，已阻止 API 启动；"
                "请先修复数据库迁移再重新发布"
            )
        if not run(selfhosted_deploy_command(ROOT), deploy_dir):
            raise SystemExit("self-hosted Docker Compose deployment failed")
        # nginx.conf 以 bind mount 方式挂载：文件变了但进程不会自动重读，
        # 新增的 location 路由会静默不生效。
        if not run(selfhosted_nginx_reload_command(ROOT), deploy_dir):
            print("[WARN] nginx reload 失败，请手动执行："
                  "docker compose exec nginx nginx -s reload", file=sys.stderr)
        print("\n[OK] 本地 Web 静态资源与 App 后端包已构建\n"
              "[OK] schema-migrations 已重跑并成功\n"
              "[OK] 已调用自建 Docker Compose 部署入口\n"
              "[OK] nginx 已 reload")
    else:
        print("\n[OK] 本地 Web 静态资源与 App 后端包已构建\n"
              "[PAUSED] 未执行部署")


if __name__ == "__main__":
    main()

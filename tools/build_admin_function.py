#!/usr/bin/env python3
"""汇组后台管理 HTTP 云函数部署包（cloudfunctions/otv-admin/）。

从 tools/ 单一来源拷入当前实现 + 品牌字标 + Web 函数引导脚本：
  python tools/build_admin_function.py
部署：manageFunctions(createFunction/updateFunctionCode, functionRootPath=cloudfunctions)
  + manageGateway(createAccess /admin → otv-admin, HTTP, 匿名网关、应用层登录门禁把关)

云端环境变量（函数配置注入，不进代码包）：
  OTV_ADMIN_USER / OTV_ADMIN_PASS  登录门禁（必填）
  OTV_ADMIN_SECRET                 会话签名密钥（可选：不设则由登录凭据派生固定值，
                                   多实例/冷启动间一致；随机兜底会致登录后立刻掉线）
  OTV_AUTH_USER  / OTV_AUTH_PASS   svc_import（必填，db_import 环境变量分支）
  OTV_PRIV_KEY                     可选：卡密签发私钥（不设则云端补货禁用，本地补货不受影响）
"""
import shutil
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
PROJ = TOOLS.parent
FUNC = PROJ / "cloudfunctions" / "otv-admin"

FILES = ("admin_server.py", "scf_main.py", "admin.html", "db_import.py", "license_gen.py")
# 影视源后台接口运行时依赖。admin_server.py 在云函数包根目录加载这些模块。
BACKEND_FILES = ("vod_sources.py", "vod_sources.default.json", "hhkan.py")

BOOTSTRAP = """#!/bin/bash
cd /var/user
export PORT=${PORT:-9000}
exec python3 scf_main.py
"""

PKG = """{
  "name": "otv-admin",
  "version": "1.20.0",
  "description": "My TV 后台管理系统（HTTP Web 函数：外网访问 + 登录门禁）"
}
"""


def main() -> None:
    FUNC.mkdir(parents=True, exist_ok=True)
    # Windows 偶发目录句柄占用（bash cwd 残留）：清内容而非删目录
    for f in FUNC.iterdir():
        if f.is_dir():
            shutil.rmtree(f, ignore_errors=True)
        else:
            f.unlink(missing_ok=True)
    for name in FILES:
        shutil.copy2(TOOLS / name, FUNC / name)
        print("cloudfunctions/otv-admin/", name)
    for name in BACKEND_FILES:
        shutil.copy2(TOOLS / "backend-src" / name, FUNC / name)
        print("cloudfunctions/otv-admin/", name)
    shutil.copy2(
        PROJ / "android" / "app" / "src" / "main" / "res" / "drawable" / "my_tv_logo.png",
        FUNC / "my_tv_logo.png")
    print("cloudfunctions/otv-admin/ my_tv_logo.png")
    (FUNC / "scf_bootstrap").write_text(BOOTSTRAP, encoding="utf-8", newline="\n")
    (FUNC / "package.json").write_text(PKG, encoding="utf-8", newline="\n")
    print("cloudfunctions/otv-admin/ scf_bootstrap + package.json")
    print("部署包就绪:", FUNC)


if __name__ == "__main__":
    main()

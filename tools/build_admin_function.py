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
import zipfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
PROJ = TOOLS.parent
FUNC = PROJ / "cloudfunctions" / "otv-admin"

FILES = ("admin_server.py", "apk_update.py", "scf_main.py", "admin.html", "db_import.py", "license_gen.py",
         "requirements.txt")
# 影视源后台接口运行时依赖。admin_server.py 在云函数包根目录加载这些模块。
BACKEND_FILES = ("vod_sources.py", "vod_sources.default.json", "hhkan.py")

BOOTSTRAP = """#!/bin/bash
cd /var/user
export PORT=${PORT:-9000}
# 云端补货（ECDSA 签发）依赖 cryptography：wheel 解包到 vendor/（与 otv-web 的 quickjs 同款做法）
export PYTHONPATH=/var/user/vendor:${PYTHONPATH}
# /var/user 只读：补货产物（cards.csv / import.sql）写到 /tmp
export OTV_OUT_DIR=/tmp/otv-out
exec python3 scf_main.py
"""

# 云端补货签名依赖（tools/vendor/*.whl，本地预下载；缺失则该功能在云端不可用）
VENDOR_WHEELS = ("cryptography-*", "cffi-*", "pycparser-*")

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
    vendor = FUNC / "vendor"
    found = []
    for pattern in VENDOR_WHEELS:
        for wheel in sorted((TOOLS / "vendor").glob(pattern + ".whl")):
            vendor.mkdir(exist_ok=True)
            with zipfile.ZipFile(wheel) as zf:
                zf.extractall(vendor)
            found.append(wheel.name)
    if found:
        print("cloudfunctions/otv-admin/ vendor/ (%d wheel): %s" % (len(found), ", ".join(found)))
    else:
        print("⚠ tools/vendor 下没找到签名依赖 wheel（云端补货会在运行时缺 cryptography）——"
              "按《开发与发布手册》§5.1 的 pip download 命令补上再汇组")
    (FUNC / "scf_bootstrap").write_text(BOOTSTRAP, encoding="utf-8", newline="\n")
    (FUNC / "package.json").write_text(PKG, encoding="utf-8", newline="\n")
    print("cloudfunctions/otv-admin/ scf_bootstrap + package.json")
    print("部署包就绪:", FUNC)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""后台管理接口冒烟：登录后逐个打真实接口，任何 error 都算失败。

后台页面能打开不代表功能可用。切栈（CloudBase → 自建）时曾漏掉
/api/logs 的数据源迁移，页面正常、日志接口 500，直到人工点开才发现。
这个脚本把"登录 + 全部只读接口"固化成一条可重复执行的检查。

用法（在任意能访问后台的机器上）：
    python tools/selfhosted/admin_smoke.py --base https://<后台地址>
    OTV_ADMIN_USER=admin OTV_ADMIN_PASS=... python tools/selfhosted/admin_smoke.py ...

凭据来源优先级：命令行参数 > 环境变量。
返回码 0 表示全部通过；非 0 表示有接口报错。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

# 后台只实现了 do_GET（/、/logo.png、/api/whoami），其余业务接口一律 POST。
READ_ENDPOINTS = [
    "/api/stats",
    "/api/licenses",
    "/api/hu_list",
    "/api/iptv_list",
    "/api/iptv_versions",
    "/api/vod_source_list",
    "/api/logs",
    "/api/ann_get",
    "/api/health",
]


def _opener() -> urllib.request.OpenerDirector:
    ctx = ssl._create_unverified_context()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        urllib.request.HTTPSHandler(context=ctx),
    )


def _call(opener, base, path, data=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        base + path,
        data=body,
        method=method or ("POST" if body is not None else "GET"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc).encode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="后台根地址，如 https://ip:port")
    ap.add_argument("--user", default=os.environ.get("OTV_ADMIN_USER", ""))
    ap.add_argument("--pass", dest="password",
                    default=os.environ.get("OTV_ADMIN_PASS", ""))
    args = ap.parse_args()
    if not args.user or not args.password:
        print("need --user/--pass or OTV_ADMIN_USER/OTV_ADMIN_PASS", file=sys.stderr)
        return 2

    base = args.base.rstrip("/")
    opener = _opener()

    status, body = _call(opener, base, "/api/login",
                         {"user": args.user, "pass": args.password})
    print("login ->", status)
    if status != 200:
        print("  body:", body[:200], file=sys.stderr)
        return 1

    status, _ = _call(opener, base, "/api/whoami")
    print("whoami ->", status)
    if status != 200:
        print("  session cookie not honored (Secure cookie over plain HTTP?)",
              file=sys.stderr)
        return 1

    failed = []
    for path in READ_ENDPOINTS:
        status, body = _call(opener, base, path, data={})
        flag = "" if status == 200 else "  <-- FAIL"
        print(f"{path} -> {status}{flag}")
        if status != 200:
            failed.append((path, status, body[:200]))
        else:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                print("   non-JSON body:", body[:120])
                continue
            if isinstance(payload, dict) and payload.get("error"):
                failed.append((path, status, str(payload["error"])[:200]))
                print("   error field:", str(payload["error"])[:160])

    if failed:
        print("\nFAILED:")
        for path, status, detail in failed:
            print(" -", path, status, detail)
        return 1
    print("\nall admin endpoints OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

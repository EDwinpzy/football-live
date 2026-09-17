"""单独增改 CloudBase 云函数环境变量（不动代码、不动内存/超时/运行时）。

为什么不用现成命令：`tcb fn deploy` / `tcb config update fn` 会按 `cloudbaserc.json` **整推**配置，
3.8.1 实测还会把变量名首字母改小写（`OTV_ADMIN_USER` → `oTV_ADMIN_USER`），照推会直接打断后台登录。
本脚本用 CloudBase CLI 已登录的临时凭据直连腾讯云 SCF `UpdateFunctionConfiguration`，
只替换 `Environment`，其余变量先取回再原样回写。

用法：
  python tools/fn_env.py show otv-admin
  python tools/fn_env.py set otv-admin OTV_PRIV_KEY=@tools/keys/private_key.pem
  python tools/fn_env.py set otv-admin OTV_ADMIN_SECRET=some-long-random-string

凭据：默认读 ~/.config/.cloudbase/auth.json（`tcb login` 写下的临时密钥）；过期就先跑一次
任意 tcb 命令刷新。只打印变量名与长度，不打印任何值。
"""
import datetime
import hashlib
import hmac
import json
import sys
import time
import urllib.request
from pathlib import Path

HOST = "scf.tencentcloudapi.com"
SERVICE = "scf"
VERSION = "2018-04-16"
REGION = "ap-shanghai"
NAMESPACE = "appletv-d5ge1bth794873f76"
ROOT = Path(__file__).resolve().parent.parent
AUTH = Path.home() / ".config" / ".cloudbase" / "auth.json"


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def call(action, payload, cred):
    body = json.dumps(payload, ensure_ascii=False)
    ts = int(time.time())
    date = datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%d")
    ctype = "application/json; charset=utf-8"
    canonical_headers = "content-type:%s\nhost:%s\nx-tc-action:%s\n" % (ctype, HOST, action.lower())
    signed_headers = "content-type;host;x-tc-action"
    hashed = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical_request = "POST\n/\n\n%s\n%s\n%s" % (canonical_headers, signed_headers, hashed)
    scope = "%s/%s/tc3_request" % (date, SERVICE)
    sts = "TC3-HMAC-SHA256\n%d\n%s\n%s" % (
        ts, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest())
    k_date = _hmac(("TC3" + cred["secret_key"]).encode("utf-8"), date)
    k_service = _hmac(k_date, SERVICE)
    k_signing = _hmac(k_service, "tc3_request")
    signature = hmac.new(k_signing, sts.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": "TC3-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                         % (cred["secret_id"], scope, signed_headers, signature),
        "Content-Type": ctype,
        "Host": HOST,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(ts),
        "X-TC-Version": VERSION,
        "X-TC-Region": REGION,
    }
    if cred.get("token"):
        headers["X-TC-Token"] = cred["token"]
    req = urllib.request.Request("https://" + HOST, data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


def creds():
    raw = json.loads(AUTH.read_text(encoding="utf-8"))
    c = raw.get("credential") or raw
    exp = int(c.get("tmpExpired") or 0)
    left = exp / 1000 - time.time()
    if left < 60:
        sys.exit("临时凭据还剩 %d 秒，先跑一次 tcb 命令（如 tcb fn list）刷新" % left)
    return {"secret_id": c["tmpSecretId"], "secret_key": c["tmpSecretKey"],
            "token": c.get("tmpToken") or c.get("tokenId") or c.get("token")}


def _variables(func, cred):
    got = call("GetFunction", {"FunctionName": func, "Namespace": NAMESPACE}, cred)
    if "Error" in got.get("Response", {}):
        sys.exit("GetFunction 失败: " + json.dumps(got["Response"]["Error"], ensure_ascii=False))
    return got["Response"], (got["Response"].get("Environment") or {}).get("Variables") or []


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("show", "set"):
        sys.exit(__doc__)
    mode, func = args[0], args[1]
    pairs = []
    for item in args[2:]:
        if "=" not in item:
            sys.exit("参数要写成 KEY=VALUE 或 KEY=@文件路径: " + item)
        key, value = item.split("=", 1)
        if value.startswith("@"):
            value = (ROOT / value[1:]).read_text(encoding="utf-8").strip()
        pairs.append((key, value))

    cred = creds()
    resp, current = _variables(func, cred)
    print("%s | Runtime=%s Timeout=%s Memory=%s" %
          (func, resp.get("Runtime"), resp.get("Timeout"), resp.get("MemorySize")))
    print("现有变量:", ", ".join("%s(%d)" % (v["Key"], len(v["Value"])) for v in current))
    if mode == "show":
        return

    variables = [{"Key": v["Key"], "Value": v["Value"]} for v in current]
    for key, value in pairs:
        for v in variables:
            if v["Key"] == key:
                v["Value"] = value
                break
        else:
            variables.append({"Key": key, "Value": value})
    out = call("UpdateFunctionConfiguration",
               {"FunctionName": func, "Namespace": NAMESPACE,
                "Environment": {"Variables": variables}}, cred)
    print("UpdateFunctionConfiguration ->", json.dumps(out.get("Response", {}), ensure_ascii=False))
    _, after = _variables(func, cred)
    print("写入后变量:", ", ".join("%s(%d)" % (v["Key"], len(v["Value"])) for v in after))


if __name__ == "__main__":
    main()

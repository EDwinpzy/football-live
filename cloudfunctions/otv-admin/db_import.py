#!/usr/bin/env python3
"""把 license_gen.py 批次目录导入 CloudBase PG（v1.17 运维工具）

鉴权：svc_import 服务账号 signin 换 2 小时 access_token 后调 import_licenses RPC
（SECURITY DEFINER + 函数内 auth.uid() 白名单，仅 svc_import 可执行）。
凭据存 tools/keys/svc_import_*.txt，属敏感文件，不入仓库不进会话正文。

用法:
  python db_import.py out/20260902-monthly-100

批次目录需含 cards.csv（note 来源）与 tickets.jsonl（票据原文），由 license_gen.py 生成。
"""
import argparse
import csv
import json
import os
import pathlib
import stat
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent
KEYS = ROOT / "keys"
CLOUDBASE_API_HOST = "appletv-d5ge1bth794873f76.api.tcloudbasegateway.com"
CLOUDBASE_SERVICE_HOST = "appletv-d5ge1bth794873f76.service.tcloudbase.com"
CLOUDBASE_BASE = f"https://{CLOUDBASE_API_HOST}"
PUBKEY_FILE = KEYS / "publishable_key.txt"


@dataclass(frozen=True)
class EndpointConfig:
    base_url: str
    auth_url: str
    rest_base: str
    storage_base: str
    public_base_url: str
    public_web_base_url: str
    hotupdate_endpoint: str
    activate_endpoint: str
    environment_name: str
    self_hosted: bool

    @property
    def storage_upload_method(self) -> str:
        return "PUT" if self.self_hosted else "POST"

    @staticmethod
    def _object_key(key: str) -> str:
        if not isinstance(key, str) or not key or "\\" in key or "\x00" in key:
            raise ValueError("invalid My TV object key")
        if key.startswith("/") or any(part in ("", ".", "..") for part in key.split("/")):
            raise ValueError("invalid My TV object key")
        return urllib.parse.quote(key, safe="/")

    def public_object_url(self, bucket: str, key: str) -> str:
        if bucket != "hotupdate" or not self.public_base_url:
            raise ValueError("public My TV origin is not configured")
        return f"{self.public_base_url}/v1/storages/object/{bucket}/{self._object_key(key)}"

    def storage_object_url(self, key: str) -> str:
        return f"{self.storage_base}/{self._object_key(key)}"

    def public_web_url(self, path: str) -> str:
        if not self.public_web_base_url:
            raise ValueError("public My TV origin is not configured")
        return f"{self.public_web_base_url}/web/{self._object_key(path)}"

    def check_url(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("target URL is not on the service allowlist") from exc
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("target URL is not on the service allowlist")
        if self.self_hosted:
            origin = (parsed.scheme, parsed.hostname, port)
            allowed = {
                ("http", "auth", 8081),
                ("http", "postgrest", 3000),
                ("http", "storage", 8092),
                ("http", "business-api", 8082),
            }
            if origin not in allowed:
                raise ValueError("target URL is not on the service allowlist")
            if parsed.hostname == "auth" and parsed.path != "/auth/v1/signin":
                raise ValueError("target URL is not on the service allowlist")
            if parsed.hostname == "storage" and not (
                parsed.path == "/v1/storages/object/hotupdate"
                or parsed.path.startswith("/v1/storages/object/hotupdate/")
            ):
                raise ValueError("target URL is not on the service allowlist")
            if parsed.hostname == "business-api" and not (
                parsed.path == "/activate" or parsed.path == "/hotupdate" or parsed.path.startswith("/hotupdate/")
            ):
                raise ValueError("target URL is not on the service allowlist")
            return
        if parsed.scheme != "https" or port is not None:
            raise ValueError("target URL is not on the service allowlist")
        if parsed.hostname == CLOUDBASE_API_HOST:
            return
        if parsed.hostname == CLOUDBASE_SERVICE_HOST and (
            parsed.path == "/activate" or parsed.path == "/hotupdate" or parsed.path.startswith("/hotupdate/")
        ):
            return
        raise ValueError("target URL is not on the service allowlist")


def _public_origin(value: str, *, self_hosted: bool) -> str:
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("public origin must be an HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("public origin must be an HTTPS origin")
    if self_hosted and ("cloudbase" in parsed.hostname.lower() or "tcloudbase" in parsed.hostname.lower()):
        raise ValueError("CloudBase URLs are forbidden in self-hosted mode")
    return value


def load_endpoint_config(environ=None) -> EndpointConfig:
    env = os.environ if environ is None else environ
    self_hosted = env.get("OTV_SELFHOSTED_MODE") == "1"
    if self_hosted:
        public = env.get("OTV_PUBLIC_BASE_URL", "")
        public = _public_origin(public, self_hosted=True) if public else ""
        config = EndpointConfig(
            base_url="http://postgrest:3000",
            auth_url=env.get("OTV_AUTH_URL", "http://auth:8081/auth/v1/signin").rstrip("/"),
            rest_base=env.get("OTV_REST_BASE", "http://postgrest:3000").rstrip("/"),
            storage_base=env.get("OTV_STORAGE_BASE", "http://storage:8092/v1/storages/object/hotupdate").rstrip("/"),
            public_base_url=public,
            public_web_base_url=public,
            hotupdate_endpoint=env.get("OTV_HOTUPDATE_ENDPOINT", "http://business-api:8082/hotupdate").rstrip("/"),
            activate_endpoint=env.get("OTV_ACTIVATE_ENDPOINT", "http://business-api:8082/activate").rstrip("/"),
            environment_name="mytv-selfhosted",
            self_hosted=True,
        )
    else:
        config = EndpointConfig(
            base_url=CLOUDBASE_BASE,
            auth_url=f"{CLOUDBASE_BASE}/auth/v1/signin",
            rest_base=f"{CLOUDBASE_BASE}/v1/rdb/rest",
            storage_base=f"{CLOUDBASE_BASE}/v1/storages/object/hotupdate",
            public_base_url=CLOUDBASE_BASE,
            public_web_base_url=f"https://{CLOUDBASE_SERVICE_HOST}",
            hotupdate_endpoint=f"https://{CLOUDBASE_SERVICE_HOST}/hotupdate",
            activate_endpoint=f"https://{CLOUDBASE_SERVICE_HOST}/activate",
            environment_name="appletv-d5ge1bth794873f76",
            self_hosted=False,
        )
    for endpoint in (config.auth_url, config.rest_base, config.storage_base, config.hotupdate_endpoint, config.activate_endpoint):
        config.check_url(endpoint)
    return config


def load_auth_credentials(environ=None) -> tuple[str, str]:
    env = os.environ if environ is None else environ
    username = env.get("OTV_AUTH_USER", "").strip()
    password = env.get("OTV_AUTH_PASS", "")
    if username and password:
        return username, password
    credential_path = env.get("OTV_AUTH_CREDENTIALS_FILE", "").strip()
    if credential_path:
        path = pathlib.Path(credential_path)
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("auth credential file permissions must be 0600 or stricter")
        data = json.loads(path.read_text(encoding="utf-8"))
        username = data.get("username") if isinstance(data, dict) else None
        password = data.get("password") if isinstance(data, dict) else None
        if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
            raise ValueError("auth credential file is invalid")
        return username.strip(), password
    return (
        (KEYS / "svc_import_user.txt").read_text(encoding="utf-8").strip(),
        (KEYS / "svc_import_pass.txt").read_text(encoding="utf-8").strip(),
    )


ENDPOINTS = load_endpoint_config()
BASE = ENDPOINTS.base_url
REST_BASE = ENDPOINTS.rest_base
AUTH_URL = ENDPOINTS.auth_url
STORAGE_BASE_URL = ENDPOINTS.storage_base
PUBLIC_BASE_URL = ENDPOINTS.public_base_url
PUBLIC_WEB_BASE_URL = ENDPOINTS.public_web_base_url
HOTUPDATE_ENDPOINT = ENDPOINTS.hotupdate_endpoint
ACTIVATE_ENDPOINT = ENDPOINTS.activate_endpoint


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "重定向被拒绝", headers, fp)


def check_url(url: str):
    ENDPOINTS.check_url(url)


def signin() -> str:
    # v1.20 云端形态（HTTP 云函数/容器）：优先环境变量注入的 svc_import 凭据，
    # 本地形态回退 keys/ 文件（不入仓库不入镜像层）
    user, pw = load_auth_credentials()
    url = AUTH_URL
    check_url(url)
    req = urllib.request.Request(
        url, data=json.dumps({"username": user, "password": pw}).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as r:
        out = json.loads(r.read().decode("utf-8"))
    tok = out.get("access_token")
    if not tok:
        sys.exit(f"signin 未返回 access_token: {list(out.keys())}")
    return tok


def main():
    p = argparse.ArgumentParser(description="导入卡密批次到 CloudBase PG")
    p.add_argument("dir", help="license_gen.py 输出的批次目录")
    args = p.parse_args()

    d = pathlib.Path(args.dir)
    if not (d / "tickets.jsonl").exists():
        sys.exit(f"找不到 {d}/tickets.jsonl")

    tickets = [json.loads(ln) for ln in (d / "tickets.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    with (d / "cards.csv").open(encoding="utf-8-sig", newline="") as f:
        cards = list(csv.DictReader(f))
    if len(tickets) != len(cards):
        sys.exit(f"cards.csv({len(cards)}) 与 tickets.jsonl({len(tickets)}) 行数不一致")

    rows = [{"code": t["code"], "plan": t["plan"], "days": t["days"],
             "note": c.get("note", ""), "ticket": t}
            for t, c in zip(tickets, cards)]

    tok = signin()
    url = REST_BASE + "/rpc/import_licenses"
    check_url(url)
    req = urllib.request.Request(
        url,
        data=json.dumps({"p_rows": rows}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as r:
            print(f"{d.name}: HTTP {r.status} → {r.read().decode('utf-8')}")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8')[:500]}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Public football-client health and hot-update API."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}
RPC_ALLOWLIST = frozenset({"hotupdate_check", "hotupdate_report"})
MAX_BODY_BYTES = 64 * 1024
CHECK_CACHE_SECONDS = 120


def _json_response(data: object, *, cache_control: str = "no-store") -> tuple[int, object, dict[str, str]]:
    return 200, data, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": cache_control,
        **CORS_HEADERS,
    }


def _not_found() -> tuple[int, object, dict[str, str]]:
    return 404, {"ret": 404, "msg": "not_found"}, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        **CORS_HEADERS,
    }


def _js_string(value: object) -> str:
    if value is None or value is False:
        return ""
    if isinstance(value, dict):
        return "[object Object]"
    if isinstance(value, list):
        return ",".join(_js_string(item) for item in value)
    if value is True:
        return "true"
    return str(value)


def _to_int(value: object, default: int | None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            return default
        number = int(value)
    else:
        match = re.match(r"^\s*([+-]?\d+)", str(value))
        if not match:
            return default
        try:
            number = int(match.group(1), 10)
        except ValueError:
            return default
    return number if 0 <= number <= 2147483647 else default


class PostgrestRpcClient:
    """Call only the hot-update RPCs over the private Compose network."""

    def __init__(self, rpc_base_url: str, *, timeout: float = 10.0):
        parsed = urlsplit(rpc_base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "postgrest"
            or parsed.port != 3000
            or parsed.path.rstrip("/") != "/rpc"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PostgREST must use the private self-hosted endpoint")
        self.rpc_base_url = rpc_base_url.rstrip("/")
        self.timeout = timeout

    def __call__(self, name: str, payload: Mapping[str, object]) -> object:
        if name not in RPC_ALLOWLIST:
            raise ValueError("RPC is not public")
        request = Request(
            f"{self.rpc_base_url}/{name}",
            data=json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise RuntimeError("RPC response is too large")
                return json.loads(raw.decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("PostgREST request failed") from exc


class BusinessApi:
    """Expose only football-client health and hot-update protocols."""

    def __init__(
        self,
        rpc: Callable[[str, Mapping[str, object]], object],
        *,
        object_root: str | Path = "/srv/objects",
        public_base_url: str = "",
        hotupdate_off: bool = False,
    ):
        self.rpc = rpc
        self.object_root = Path(object_root).resolve()
        self.public_base_url = self._validate_public_base(public_base_url)
        self.hotupdate_off = bool(hotupdate_off)
        self._cache: dict[tuple[object, ...], tuple[float, object]] = {}
        self._cache_lock = threading.Lock()

    @staticmethod
    def _validate_public_base(value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("public API origin must be an HTTPS origin")
        if "tcloudbase" in parsed.hostname.lower() or "cloudbase" in parsed.hostname.lower():
            raise ValueError("CloudBase URLs cannot be used by the self-hosted API")
        if parsed.path not in ("", "/"):
            raise ValueError("public API origin must not contain a path")
        return value

    def _cached_rpc(self, key: tuple[object, ...], name: str, payload: Mapping[str, object]) -> object:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < CHECK_CACHE_SECONDS:
                return copy.deepcopy(cached[1])
        result = self.rpc(name, payload)
        with self._cache_lock:
            if len(self._cache) > 100:
                self._cache.clear()
            self._cache[key] = (time.monotonic(), copy.deepcopy(result))
        return result

    @staticmethod
    def _device_id(body: Mapping[str, object]) -> str | None:
        value = _js_string(body.get("deviceId", "")).strip()
        return value if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", value) else None

    def _object_path(self, key: str) -> Path:
        if not key or "\\" in key or "\x00" in key:
            raise ValueError("invalid object key")
        if key.startswith("/") or any(part in ("", ".", "..") for part in key.split("/")):
            raise ValueError("invalid object key")
        path = (self.object_root / "hotupdate" / Path(*key.split("/"))).resolve()
        path.relative_to(self.object_root)
        return path

    def handle(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, object, dict[str, str]]:
        del headers  # Forwarding headers are not used by the retained public protocols.
        method = method.upper()
        path = urlsplit(path).path
        if method == "OPTIONS":
            return 204, b"", dict(CORS_HEADERS)
        if path in ("/health", "/healthz"):
            return _json_response({"status": "ok"}) if method == "GET" else _not_found()

        if path not in ("/hotupdate/check", "/check", "/hotupdate/report", "/report") or method != "POST":
            return _not_found()
        if body is None or not isinstance(body, Mapping):
            return _json_response({"ret": 400, "msg": "bad_json"})
        device_id = self._device_id(body)
        if device_id is None:
            return _json_response({"ret": 400, "msg": "bad_device"})

        if path in ("/hotupdate/check", "/check"):
            apk_version = _to_int(body.get("apkVersion"), 0)
            if apk_version <= 0:
                return _json_response({"ret": 400, "msg": "bad_apk"})
            if self.hotupdate_off:
                return _json_response({"ret": 0, "hasUpdate": False, "blackout": True})
            hot_version = _to_int(body.get("hotVersion"), 0)
            try:
                out = copy.deepcopy(self._cached_rpc(
                    ("check", hot_version, apk_version),
                    "hotupdate_check",
                    {"p_hot_version": hot_version, "p_apk_version": apk_version},
                ))
                if isinstance(out, dict) and out.get("hasUpdate") and isinstance(out.get("pkg"), dict):
                    key = out["pkg"].get("objectKey")
                    if isinstance(key, str) and key:
                        self._object_path(key)
                        if self.public_base_url:
                            out["pkg"]["url"] = (
                                f"{self.public_base_url}/v1/storages/object/hotupdate/"
                                f"{quote(key, safe='/')}"
                            )
                return _json_response(out)
            except Exception:
                return _json_response({"ret": 502, "msg": "upstream_error"})

        payload = {
            "p_device": device_id,
            "p_apk_version": _to_int(body.get("apkVersion"), None),
            "p_from_version": _to_int(body.get("fromVersion"), None),
            "p_to_version": _to_int(body.get("toVersion"), None),
            "p_result": _js_string(body.get("result", ""))[:32],
            "p_detail": _js_string(body.get("detail", ""))[:500],
        }
        try:
            return _json_response(self.rpc("hotupdate_report", payload))
        except Exception:
            return _json_response({"ret": 502, "msg": "upstream_error"})


def create_server(api: BusinessApi, host: str = "0.0.0.0", port: int = 8082) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "FootballLiveBusinessApi/1.0"

        def _dispatch(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0 or length > MAX_BODY_BYTES:
                    self._write(*_json_response({"ret": 413, "msg": "body_too_large"}))
                    return
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeError, json.JSONDecodeError):
                body = None
            self._write(*api.handle(self.command, self.path, body, self.headers))

        def _write(self, status: int, body: object, headers: Mapping[str, str]) -> None:
            if isinstance(body, bytes):
                raw = body
            elif isinstance(body, str):
                raw = body.encode("utf-8")
            else:
                raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if self.command != "HEAD" and status != 204:
                self.wfile.write(raw)

        def do_GET(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

        def do_OPTIONS(self):
            self._dispatch()

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    rpc_url = os.environ.get("OTV_POSTGREST_RPC_URL", "http://postgrest:3000/rpc")
    public_base_url = os.environ.get("OTV_PUBLIC_BASE_URL", "")
    object_root = os.environ.get("OTV_STORAGE_ROOT", "/srv/objects")
    hotupdate_off = os.environ.get("OTV_HU_OFF", "") == "1"
    rpc = PostgrestRpcClient(rpc_url)
    api = BusinessApi(
        rpc,
        object_root=object_root,
        public_base_url=public_base_url,
        hotupdate_off=hotupdate_off,
    )
    host = os.environ.get("OTV_API_BIND", "0.0.0.0")
    port = int(os.environ.get("OTV_API_PORT", "8082"))
    create_server(api, host, port).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

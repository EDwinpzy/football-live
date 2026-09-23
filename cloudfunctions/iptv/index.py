#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容旧版 My TV 的 IPTV 读取入口；写入与定时重建归新服务器所有。"""

import os
import ssl
from urllib import request as urlreq
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit


SELFHOSTED_BASE = os.environ.get("OTV_SELFHOSTED_BASE_URL", "https://103.236.97.137:35208").rstrip("/")
CLOUDBASE_SUFFIXES = (".tcloudbase.com", ".tcloudbasegateway.com")
MAX_PLAYLIST_BYTES = 16 * 1024 * 1024


class _NoBridgeRedirect(urlreq.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _response(status, body, content_type, extra_headers=None):
    headers = {
        "Content-Type": content_type,
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Range, If-Range, If-None-Match, If-Modified-Since",
    }
    headers.update(extra_headers or {})
    return {
        "statusCode": int(status),
        "headers": headers,
        "body": body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body),
        "isBase64Encoded": False,
    }


def _proxy_latest(event):
    request_context = event.get("requestContext") or {}
    context_http = request_context.get("http") or {}
    method = str(event.get("httpMethod") or event.get("method") or
                 context_http.get("method") or request_context.get("httpMethod") or "GET").upper()
    raw_path = event.get("rawPath") or event.get("path") or request_context.get("path") or "/"
    parsed = urlsplit(str(raw_path))
    if method == "OPTIONS" and parsed.path in ("/", "/latest", "/iptv/latest"):
        return _response(204, b"", "text/plain; charset=utf-8")
    if method != "GET" or parsed.path not in ("/", "/latest", "/iptv/latest"):
        return _response(404, b"not found", "text/plain; charset=utf-8")

    upstream = urlsplit(SELFHOSTED_BASE)
    if (upstream.scheme != "https" or not upstream.hostname or upstream.username or upstream.password
            or upstream.query or upstream.fragment or upstream.path not in ("", "/")
            or any(upstream.hostname.endswith(suffix) for suffix in CLOUDBASE_SUFFIXES)):
        raise ValueError("invalid self-hosted origin")

    query = event.get("rawQueryString") or parsed.query
    if not query:
        params = event.get("queryStringParameters") or {}
        if isinstance(params, dict):
            query = urlencode(params, doseq=True)
    target = SELFHOSTED_BASE + "/iptv/latest" + ("?" + query if query else "")

    input_headers = event.get("headers") or {}
    input_headers = {str(name).lower(): value for name, value in input_headers.items()}
    request_headers = {
        name: str(input_headers[name])
        for name in ("accept", "range", "if-range", "if-none-match", "if-modified-since", "user-agent")
        if input_headers.get(name)
    }
    ca_file = os.path.join(os.path.dirname(__file__), "mytv_migration_ca.pem")
    tls_context = ssl.create_default_context(cafile=ca_file)
    request = urlreq.Request(target, headers=request_headers, method="GET")
    opener = urlreq.build_opener(_NoBridgeRedirect, urlreq.HTTPSHandler(context=tls_context))
    try:
        response = opener.open(request, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        body = response.read(MAX_PLAYLIST_BYTES + 1)
        if len(body) > MAX_PLAYLIST_BYTES:
            return _response(502, b"playlist response too large", "text/plain; charset=utf-8")
        status = getattr(response, "status", 200)
        content_type = response.headers.get("Content-Type", "audio/x-mpegurl; charset=utf-8")
        passthrough = {}
        for name in ("Cache-Control", "ETag", "Last-Modified", "Content-Range", "Accept-Ranges"):
            value = response.headers.get(name)
            if value:
                passthrough[name] = value
    return _response(status, body, content_type, passthrough)


def main_handler(event, context):
    """CloudBase 兼容代理；历史定时写入任务明确退役。"""
    if isinstance(event, dict) and (event.get("Type") == "Timer" or event.get("TriggerName")):
        return {"ret": 0, "retired": True, "msg": "IPTV rebuild is owned by the self-hosted service"}
    try:
        return _proxy_latest(event if isinstance(event, dict) else {})
    except Exception as exc:  # noqa: BLE001
        print("[iptv-bridge] request failed:", type(exc).__name__)
        return _response(502, b"upstream unavailable", "text/plain; charset=utf-8")


def create_server(host="0.0.0.0", port=None):
    """SCF Web 函数必须在容器里监听端口；这里把 HTTP 请求转成平台事件。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self):
            length = 0
            raw_length = self.headers.get("Content-Length")
            if raw_length:
                try:
                    length = max(0, int(raw_length))
                except ValueError:
                    length = 0
            if length:
                self.rfile.read(length)
            event = {
                "httpMethod": self.command,
                "path": self.path,
                "headers": {name: value for name, value in self.headers.items()},
            }
            result = main_handler(event, None)
            body = str(result.get("body") or "").encode("utf-8")
            self.send_response(int(result.get("statusCode") or 200))
            for name, value in (result.get("headers") or {}).items():
                self.send_header(name, str(value))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._dispatch()

        def do_HEAD(self):  # noqa: N802
            self._dispatch()

        def do_OPTIONS(self):  # noqa: N802
            self._dispatch()

        def log_message(self, fmt, *args):  # noqa: A003
            return

    return ThreadingHTTPServer((host, int(port or os.environ.get("PORT") or 9000)), Handler)


if __name__ == "__main__":
    print("[iptv-bridge] ready")
    create_server().serve_forever()

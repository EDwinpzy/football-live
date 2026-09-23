#!/usr/bin/env python3
"""Temporary CloudBase admin compatibility proxy for the self-hosted My TV server."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from pathlib import Path


DEFAULT_SELFHOSTED_ORIGIN = "https://103.236.97.137:35208"
CLOUDBASE_SUFFIXES = (".tcloudbase.com", ".tcloudbasegateway.com")
MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
ALLOWED_METHODS = frozenset({"GET", "POST", "OPTIONS"})
FORWARD_REQUEST_HEADERS = (
    "accept",
    "content-type",
    "cookie",
    "if-modified-since",
    "if-none-match",
    "if-range",
    "origin",
    "range",
    "referer",
    "user-agent",
)
FORWARD_RESPONSE_HEADERS = (
    "accept-ranges",
    "cache-control",
    "content-disposition",
    "content-range",
    "content-type",
    "etag",
    "last-modified",
    "location",
    "retry-after",
    "set-cookie",
    "www-authenticate",
)


def validate_upstream_origin(value: str) -> str:
    """Require one HTTPS origin and prevent a proxy loop back to CloudBase."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("self-hosted upstream must be an HTTPS origin") from exc
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port == 0
        or any(hostname.endswith(suffix) for suffix in CLOUDBASE_SUFFIXES)
    ):
        raise ValueError("self-hosted upstream must be an HTTPS origin outside CloudBase")
    return f"https://{parsed.netloc}".rstrip("/")


def _resolve_admin_target(method: str, request_target: str) -> str | None:
    """Map the legacy /admin gateway path to the equivalent VPS admin path."""
    if method.upper() not in ALLOWED_METHODS:
        return None
    try:
        parsed = urlsplit(request_target)
        path = unquote(parsed.path, errors="strict")
    except (UnicodeError, ValueError):
        return None
    if parsed.scheme or parsed.netloc or "\\" in path or "\x00" in path:
        return None
    if path == "/admin":
        path = "/"
    elif path.startswith("/admin/"):
        path = path[len("/admin"):]
    parts = path.split("/")[1:]
    if any(part in (".", "..") for part in parts):
        return None
    if path not in ("/", "/favicon.ico", "/logo.png", "/api") and not path.startswith("/api/"):
        return None
    return path + ("?" + parsed.query if parsed.query else "")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def create_proxy_server(
    upstream_origin: str,
    *,
    ssl_context: ssl.SSLContext | None = None,
    max_request_bytes: int = MAX_REQUEST_BYTES,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """Create an admin-only reverse proxy; loopback HTTP is useful for contract tests."""
    parsed = urlsplit(upstream_origin)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("proxy upstream must be an origin")
    origin = upstream_origin.rstrip("/")
    opener = build_opener(_NoRedirect(), HTTPSHandler(context=ssl_context))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_error(self, status: int, message: str) -> None:
            raw = json.dumps({"error": message}, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)
            self.close_connection = True

        def _dispatch(self) -> None:
            method = self.command.upper()
            target = _resolve_admin_target(method, self.path)
            if target is None:
                self._send_error(404, "not_found")
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send_error(400, "invalid_content_length")
                return
            if length < 0 or length > self.server.max_request_bytes:
                self._send_error(413, "request_too_large")
                return
            body = self.rfile.read(length) if length else b""
            if len(body) != length:
                self._send_error(400, "incomplete_request_body")
                return

            headers = {
                name: self.headers.get(name)
                for name in FORWARD_REQUEST_HEADERS
                if self.headers.get(name) is not None
            }
            headers["X-Forwarded-Proto"] = "https"
            upstream_request = Request(
                origin + target,
                data=body if method == "POST" else None,
                headers=headers,
                method=method,
            )
            try:
                try:
                    response = opener.open(upstream_request, timeout=30)
                except HTTPError as error:
                    response = error
                with response:
                    response_body = response.read(self.server.max_response_bytes + 1)
                    if len(response_body) > self.server.max_response_bytes:
                        self._send_error(502, "upstream_response_too_large")
                        return
                    status = int(getattr(response, "status", response.code))
                    response_headers = response.headers
                    self.send_response(status)
                    for name in FORWARD_RESPONSE_HEADERS:
                        values = response_headers.get_all(name) or []
                        for value in values:
                            self.send_header(name, value)
                    self.send_header("Content-Length", str(len(response_body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if method != "HEAD" and status not in (204, 304):
                        self.wfile.write(response_body)
                    self.close_connection = True
            except (OSError, URLError, TimeoutError, ssl.SSLError):
                if not self.wfile.closed:
                    self._send_error(502, "upstream_unavailable")

        def do_GET(self):  # noqa: N802
            self._dispatch()

        def do_POST(self):  # noqa: N802
            self._dispatch()

        def do_OPTIONS(self):  # noqa: N802
            self._dispatch()

        def log_message(self, fmt, *args):  # noqa: A003
            # Never log admin paths, cookies, query data, or request bodies.
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.max_request_bytes = max_request_bytes
    server.max_response_bytes = max_response_bytes
    return server


def main() -> int:
    origin = validate_upstream_origin(os.environ.get("OTV_SELFHOSTED_BASE_URL", DEFAULT_SELFHOSTED_ORIGIN))
    ca_path = Path(__file__).resolve().with_name("mytv_migration_ca.pem")
    context = ssl.create_default_context(cafile=str(ca_path))
    server = create_proxy_server(
        origin,
        ssl_context=context,
        host="0.0.0.0",
        port=int(os.environ.get("PORT") or "9000"),
    )
    print("[otv-admin-bridge] ready")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

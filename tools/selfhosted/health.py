#!/usr/bin/env python3
"""Minimal health endpoint for the self-hosted Compose stack."""

from __future__ import annotations

import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping


def dependencies_available(env: Mapping[str, str]) -> bool:
    host = (env.get("DATABASE_HOST") or "").strip()
    objects_root = (env.get("OBJECTS_ROOT") or "").strip()
    try:
        port = int(env.get("DATABASE_PORT", ""))
    except (TypeError, ValueError):
        return False
    if not host or not objects_root or not 1 <= port <= 65535:
        return False

    root = Path(objects_root)
    if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        return False
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def make_server(host: str, port: int, env: Mapping[str, str]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/health":
                self.send_error(404)
                return
            available = dependencies_available(env)
            body = json.dumps(
                {"status": "ok" if available else "unavailable"},
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(200 if available else 503)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    server = make_server(
        os.environ.get("HEALTH_BIND", "0.0.0.0"),
        int(os.environ.get("HEALTH_PORT", "8080")),
        os.environ,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()

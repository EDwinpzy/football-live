#!/usr/bin/env python3
"""Private file API for the QiuJiLive update object store."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Collection
from urllib.parse import unquote, urlsplit

try:
    from .auth_service import decode_access_token
except ImportError:  # Running as a standalone script inside the container.
    from auth_service import decode_access_token


OBJECT_PREFIX = "/v1/storages/object/"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


class RangeNotSatisfiable(ValueError):
    pass


def safe_object_path(
    root: str | Path,
    bucket: str,
    key: str,
    allowed_buckets: Collection[str],
) -> Path:
    if bucket not in allowed_buckets or not key or "\\" in key or "\x00" in key:
        raise ValueError("invalid object path")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("invalid object path")
    relative = PurePosixPath(*parts)
    if relative.is_absolute():
        raise ValueError("invalid object path")
    root_path = Path(root).resolve()
    target = (root_path / bucket / Path(*relative.parts)).resolve()
    try:
        target.relative_to(root_path)
    except ValueError as exc:
        raise ValueError("invalid object path") from exc
    return target


def parse_byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or size <= 0:
        raise RangeNotSatisfiable("invalid range")
    first, last = match.groups()
    if not first:
        suffix = int(last or "0")
        if suffix <= 0:
            raise RangeNotSatisfiable("invalid suffix range")
        return max(0, size - suffix), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise RangeNotSatisfiable("range is outside the object")
    return start, end


def make_server(
    host: str,
    port: int,
    root: str | Path,
    allowed_buckets: Collection[str],
    writer_subjects: Collection[str],
    jwt_secret: bytes,
    *,
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
) -> ThreadingHTTPServer:
    root_path = Path(root).resolve()
    if not isinstance(jwt_secret, bytes) or len(jwt_secret) < 32:
        raise ValueError("JWT signing key must contain at least 32 bytes")
    if not allowed_buckets or any(not re.fullmatch(r"[a-z0-9_-]{1,63}", b) for b in allowed_buckets):
        raise ValueError("invalid bucket allowlist")
    writers = frozenset(str(subject) for subject in writer_subjects)
    if not writers or max_upload_bytes <= 0:
        raise ValueError("invalid storage service authorization configuration")

    class Handler(BaseHTTPRequestHandler):
        server_version = "QiuJiLiveStorage/1.0"

        def _send_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _object_path(self) -> tuple[str, str, Path]:
            encoded_path = urlsplit(self.path).path
            if not encoded_path.startswith(OBJECT_PREFIX):
                raise ValueError("invalid object route")
            decoded = unquote(encoded_path[len(OBJECT_PREFIX):], errors="strict")
            bucket, sep, key = decoded.partition("/")
            if not sep:
                raise ValueError("invalid object route")
            return bucket, key, safe_object_path(root_path, bucket, key, allowed_buckets)

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            scheme, _, token = auth.partition(" ")
            if scheme.lower() != "bearer" or not token:
                return False
            claims = decode_access_token(token, jwt_secret)
            return bool(claims and claims.get("sub") in writers)

        def do_GET(self) -> None:
            self._serve_object(head_only=False)

        def do_HEAD(self) -> None:
            self._serve_object(head_only=True)

        def _serve_object(self, *, head_only: bool) -> None:
            try:
                _, key, path = self._object_path()
            except (ValueError, UnicodeError):
                self._send_json(404, {"error": "not_found"})
                return
            if not path.is_file():
                self._send_json(404, {"error": "not_found"})
                return
            size = path.stat().st_size
            try:
                byte_range = parse_byte_range(self.headers.get("Range"), size)
            except (ValueError, RangeNotSatisfiable):
                self.send_response(416)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = byte_range if byte_range else (0, max(0, size - 1))
            length = 0 if size == 0 else end - start + 1
            status = 206 if byte_range else 200
            content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            if byte_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head_only or length == 0:
                return
            try:
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return

        def do_PUT(self) -> None:
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                _, key, path = self._object_path()
            except (ValueError, UnicodeError):
                self._send_json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0:
                self._send_json(411, {"error": "content_length_required"})
                return
            if length > max_upload_bytes:
                self._send_json(413, {"error": "object_too_large"})
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_name: str | None = None
            digest = hashlib.sha256()
            remaining = length
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".upload-", delete=False) as stream:
                    temp_name = stream.name
                    while remaining:
                        chunk = self.rfile.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise OSError("incomplete upload")
                        stream.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                os.replace(temp_name, path)
                temp_name = None
            except OSError:
                if temp_name:
                    Path(temp_name).unlink(missing_ok=True)
                self._send_json(400, {"error": "upload_failed"})
                return
            self._send_json(201, {"key": key, "size": length, "sha256": digest.hexdigest()})

        def do_DELETE(self) -> None:
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                _, _, path = self._object_path()
                path.unlink()
            except (ValueError, UnicodeError, FileNotFoundError, IsADirectoryError):
                self._send_json(404, {"error": "not_found"})
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    host = os.environ.get("OTV_STORAGE_BIND", "127.0.0.1")
    port = int(os.environ.get("OTV_STORAGE_PORT", "8092"))
    root = os.environ.get("OTV_STORAGE_ROOT", "/srv/objects")
    secret_file = Path(os.environ.get("OTV_JWT_SECRET_FILE", "/run/secrets/jwt-signing-key"))
    subjects_value = os.environ.get(
        "OTV_STORAGE_WRITER_SUBJECTS",
        os.environ.get("OTV_STORAGE_ADMIN_SUBJECTS", ""),
    )
    writer_subjects = [item.strip() for item in subjects_value.split(",") if item.strip()]
    allowed_buckets = [item.strip() for item in os.environ.get("OTV_STORAGE_BUCKETS", "hotupdate").split(",") if item.strip()]
    secret = secret_file.read_bytes().strip()
    server = make_server(host, port, root, allowed_buckets, writer_subjects, secret)
    server.serve_forever()


if __name__ == "__main__":
    main()

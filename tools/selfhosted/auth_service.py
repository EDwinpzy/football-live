#!/usr/bin/env python3
"""Small service-login endpoint that issues PostgREST-compatible JWTs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping


ISSUER = "mytv-selfhosted"
PASSWORD_ITERATIONS = 310_000
MAX_BODY_BYTES = 16 * 1024
DUMMY_SALT = "4b18af164a4f8c0ea716636b2c2af94f"


def hash_password(password: str, salt: str) -> str:
    """Return a PBKDF2-HMAC-SHA256 password digest as lowercase hex."""
    if not isinstance(password, str) or len(password) > 4096:
        raise ValueError("invalid password")
    try:
        salt_bytes = bytes.fromhex(salt)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid salt") from exc
    if len(salt_bytes) < 16:
        raise ValueError("salt must contain at least 16 bytes")
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt_bytes, PASSWORD_ITERATIONS
    ).hex()


def verify_password(
    users: Mapping[str, Mapping[str, str]], username: str, password: str
) -> Mapping[str, str] | None:
    """Verify an account without exposing whether its username exists."""
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    if len(username) > 256 or len(password) > 4096:
        return None
    account = users.get(username)
    salt = account.get("salt", DUMMY_SALT) if account else DUMMY_SALT
    expected = account.get("password_hash", "0" * 64) if account else "0" * 64
    try:
        actual = hash_password(password, salt)
    except ValueError:
        return None
    valid = hmac.compare_digest(actual, expected)
    subject = account.get("subject", "") if account else ""
    return account if valid and isinstance(subject, str) and subject else None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_access_token(
    subject: str,
    secret: bytes,
    *,
    now: int | None = None,
    ttl_seconds: int = 7200,
) -> str:
    if not isinstance(subject, str) or not subject:
        raise ValueError("subject is required")
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("JWT signing key must contain at least 32 bytes")
    if not 60 <= ttl_seconds <= 86_400:
        raise ValueError("token lifetime is outside the allowed range")
    issued_at = int(time.time() if now is None else now)
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "aud": "authenticated",
        "exp": issued_at + ttl_seconds,
        "iat": issued_at,
        "iss": ISSUER,
        "role": "authenticated",
        "sub": subject,
    }
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}.{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    signature = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def decode_access_token(
    token: str, secret: bytes, *, now: int | None = None
) -> dict[str, object] | None:
    """Validate a locally issued token; invalid/expired tokens return None."""
    if not isinstance(token, str) or len(token) > 8192:
        return None
    if not isinstance(secret, bytes) or len(secret) < 32:
        return None
    try:
        header_part, claims_part, signature_part = token.split(".")
        signing_input = f"{header_part}.{claims_part}"
        signature = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
        expected = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        header = json.loads(base64.urlsafe_b64decode(header_part + "=" * (-len(header_part) % 4)))
        claims = json.loads(base64.urlsafe_b64decode(claims_part + "=" * (-len(claims_part) % 4)))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    current_time = int(time.time() if now is None else now)
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        return None
    if not isinstance(claims, dict) or claims.get("iss") != ISSUER:
        return None
    if claims.get("role") != "authenticated" or not isinstance(claims.get("sub"), str):
        return None
    if not isinstance(claims.get("exp"), int) or claims["exp"] <= current_time:
        return None
    if not isinstance(claims.get("iat"), int) or claims["iat"] > current_time + 60:
        return None
    return claims


def load_configuration(
    users_file: str | Path, secret_file: str | Path
) -> tuple[dict[str, dict[str, str]], bytes]:
    users = json.loads(Path(users_file).read_text(encoding="utf-8"))
    if not isinstance(users, dict) or not users:
        raise ValueError("account file must contain a non-empty JSON object")
    for username, account in users.items():
        if not isinstance(username, str) or not isinstance(account, dict):
            raise ValueError("invalid account record")
        if not all(isinstance(account.get(key), str) and account[key] for key in ("subject", "salt", "password_hash")):
            raise ValueError("invalid account record")
    secret = Path(secret_file).read_bytes().strip()
    if len(secret) < 32:
        raise ValueError("JWT signing key must contain at least 32 bytes")
    return users, secret


def make_server(
    host: str,
    port: int,
    users: Mapping[str, Mapping[str, str]],
    secret: bytes,
    *,
    ttl_seconds: int = 7200,
) -> ThreadingHTTPServer:
    if not users or not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("invalid authentication configuration")
    if not 60 <= ttl_seconds <= 86_400:
        raise ValueError("token lifetime is outside the allowed range")

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: Mapping[str, object]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/auth/v1/signin":
                self._send_json(404, {"error": "not_found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                size = -1
            if not 0 <= size <= MAX_BODY_BYTES:
                self._send_json(400, {"error": "invalid_request"})
                return
            try:
                payload = json.loads(self.rfile.read(size))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid_request"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "invalid_request"})
                return
            account = verify_password(users, payload.get("username"), payload.get("password"))
            if account is None:
                self._send_json(401, {"error": "invalid_credentials"})
                return
            token = make_access_token(
                account["subject"], secret, ttl_seconds=ttl_seconds
            )
            self._send_json(
                200,
                {
                    "access_token": token,
                    "token_type": "bearer",
                    "expires_in": ttl_seconds,
                    "user": {"id": account["subject"]},
                },
            )

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    try:
        users, secret = load_configuration(
            os.environ.get("OTV_AUTH_USERS_FILE", "/run/secrets/auth-users.json"),
            os.environ.get("OTV_JWT_SECRET_FILE", "/run/secrets/jwt-signing-key"),
        )
        server = make_server(
            os.environ.get("OTV_AUTH_BIND", "0.0.0.0"),
            int(os.environ.get("OTV_AUTH_PORT", "8081")),
            users,
            secret,
            ttl_seconds=int(os.environ.get("OTV_AUTH_TOKEN_TTL", "7200")),
        )
    except (OSError, ValueError, json.JSONDecodeError):
        print("authentication service configuration is invalid", file=sys.stderr)
        raise SystemExit(2)
    server.serve_forever()


if __name__ == "__main__":
    main()

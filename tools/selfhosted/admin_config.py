"""Read runtime-only credentials for the self-hosted My TV admin service."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Mapping


def _read_private_file(path_value: str) -> str:
    path = Path(path_value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("runtime secret file is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("runtime secret file must be a regular file")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("runtime secret file permissions must be 0600 or stricter")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("runtime secret file is unavailable") from exc
    if not raw or len(raw) > 16 * 1024:
        raise ValueError("runtime secret file has an invalid size")
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("runtime secret file is invalid") from exc


def load_admin_credentials(environ: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    env = os.environ if environ is None else environ
    username = env.get("OTV_ADMIN_USER", "").strip()
    password = env.get("OTV_ADMIN_PASS", "")
    if username or password:
        if not username or not password:
            raise ValueError("OTV_ADMIN_USER and OTV_ADMIN_PASS must both be set")
        if len(username) > 256 or len(password) > 4096:
            raise ValueError("admin credentials have an invalid size")
        return username, password

    path = env.get("OTV_ADMIN_CREDENTIALS_FILE", "").strip()
    if not path:
        return None
    try:
        payload = json.loads(_read_private_file(path))
    except json.JSONDecodeError as exc:
        raise ValueError("admin credentials file is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("admin credentials file is invalid")
    username = payload.get("username")
    password = payload.get("password")
    if (
        not isinstance(username, str)
        or not username.strip()
        or not isinstance(password, str)
        or not password
        or len(username) > 256
        or len(password) > 4096
    ):
        raise ValueError("admin credentials file is invalid")
    return username.strip(), password


def load_session_secret(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    value = env.get("OTV_ADMIN_SECRET", "")
    if value:
        secret = value
    else:
        path = env.get("OTV_ADMIN_SECRET_FILE", "").strip()
        if not path:
            return None
        secret = _read_private_file(path)
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("admin session secret must contain at least 32 bytes")
    return secret

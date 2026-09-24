"""HTTP clients and URL policy for the self-hosted My TV services."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


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
    environment_name: str = "mytv-selfhosted"
    self_hosted: bool = True

    @property
    def storage_upload_method(self) -> str:
        return "PUT"

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
        allowed = {
            ("http", "auth", 8081),
            ("http", "postgrest", 3000),
            ("http", "storage", 8092),
            ("http", "business-api", 8082),
        }
        if (parsed.scheme, parsed.hostname, port) not in allowed:
            raise ValueError("target URL is not on the service allowlist")
        if parsed.hostname == "auth" and parsed.path != "/auth/v1/signin":
            raise ValueError("target URL is not on the service allowlist")
        if parsed.hostname == "storage" and not (
            parsed.path == "/v1/storages/object/hotupdate"
            or parsed.path.startswith("/v1/storages/object/hotupdate/")
        ):
            raise ValueError("target URL is not on the service allowlist")
        if parsed.hostname == "business-api" and not (
            parsed.path == "/activate"
            or parsed.path == "/hotupdate"
            or parsed.path.startswith("/hotupdate/")
        ):
            raise ValueError("target URL is not on the service allowlist")


def _public_origin(value: str) -> str:
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
        or "cloudbase" in parsed.hostname.lower()
        or "tcloudbase" in parsed.hostname.lower()
    ):
        raise ValueError("public origin must be an HTTPS origin")
    return value


def load_endpoint_config(environ=None) -> EndpointConfig:
    env = os.environ if environ is None else environ
    public = env.get("OTV_PUBLIC_BASE_URL", "")
    public = _public_origin(public) if public else ""
    config = EndpointConfig(
        base_url=env.get("OTV_REST_BASE", "http://postgrest:3000").rstrip("/"),
        auth_url=env.get("OTV_AUTH_URL", "http://auth:8081/auth/v1/signin").rstrip("/"),
        rest_base=env.get("OTV_REST_BASE", "http://postgrest:3000").rstrip("/"),
        storage_base=env.get("OTV_STORAGE_BASE", "http://storage:8092/v1/storages/object/hotupdate").rstrip("/"),
        public_base_url=public,
        public_web_base_url=public,
        hotupdate_endpoint=env.get("OTV_HOTUPDATE_ENDPOINT", "http://business-api:8082/hotupdate").rstrip("/"),
        activate_endpoint=env.get("OTV_ACTIVATE_ENDPOINT", "http://business-api:8082/activate").rstrip("/"),
    )
    for endpoint in (
        config.auth_url,
        config.rest_base,
        config.storage_base,
        config.hotupdate_endpoint,
        config.activate_endpoint,
    ):
        config.check_url(endpoint)
    return config


def _private_json_file(path_value: str) -> dict:
    path = pathlib.Path(path_value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("runtime credential file is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("runtime credential file must be a regular file")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("runtime credential file permissions must be 0600 or stricter")
    if info.st_size < 1 or info.st_size > 16 * 1024:
        raise ValueError("runtime credential file has an invalid size")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime credential file is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("runtime credential file is invalid")
    return payload


def load_auth_credentials(environ=None) -> tuple[str, str]:
    env = os.environ if environ is None else environ
    username = env.get("OTV_AUTH_USER", "").strip()
    password = env.get("OTV_AUTH_PASS", "")
    if username or password:
        if not username or not password:
            raise ValueError("OTV_AUTH_USER and OTV_AUTH_PASS must both be set")
        return username, password
    credential_path = env.get("OTV_AUTH_CREDENTIALS_FILE", "").strip()
    if credential_path:
        payload = _private_json_file(credential_path)
        username = payload.get("username")
        password = payload.get("password")
        if (
            not isinstance(username, str)
            or not username.strip()
            or not isinstance(password, str)
            or not password
        ):
            raise ValueError("runtime credential file is invalid")
        return username.strip(), password
    raise ValueError("self-hosted API credentials are not configured")


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
        raise urllib.error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


def check_url(url: str) -> None:
    ENDPOINTS.check_url(url)


def signin() -> str:
    user, password = load_auth_credentials()
    check_url(AUTH_URL)
    request = urllib.request.Request(
        AUTH_URL,
        data=json.dumps({"username": user, "password": password}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    token = result.get("access_token")
    if not token:
        raise RuntimeError("self-hosted sign-in did not return an access token")
    return token

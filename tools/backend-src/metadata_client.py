"""Read-only client for the self-hosted metadata center detail endpoint."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


class MetadataUnavailable(RuntimeError):
    """The configured metadata center could not provide a usable detail record."""


class MetadataIdentityConflict(ValueError):
    """The detail response did not identify the requested canonical My TV item."""


_DIGITS = re.compile(r"[0-9]+\Z")
MAX_DETAIL_BYTES = 1024 * 1024


def explicit_douban_id(detail):
    """Return only a single explicit ``external_ids.douban`` value.

    This deliberately does not inspect legacy IDs, titles, aliases, or any other
    field. A list or malformed value is ambiguous and is therefore unusable.
    """
    external_ids = detail.get("external_ids") if isinstance(detail, dict) else None
    value = external_ids.get("douban") if isinstance(external_ids, dict) else None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    result = str(value)
    return result if _DIGITS.fullmatch(result) else None


def _positive_id(value):
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not _DIGITS.fullmatch(text):
        return None
    number = int(text)
    return number if number > 0 else None


class MetadataClient:
    """Fetch ``/metadata/{mytv_id}`` using the configured metadata API origin."""

    def __init__(self, base_url=None, timeout=3.0, opener=None):
        self.base_url = (os.environ.get("OTV_METADATA_API_URL", "") if base_url is None
                         else str(base_url)).strip().rstrip("/")
        self.timeout = float(timeout)
        self._opener = opener or urllib.request.urlopen

    def detail(self, mytv_id):
        requested_id = _positive_id(mytv_id)
        if requested_id is None:
            raise ValueError("mytv_id must be a positive integer")

        origin = self._validated_origin()
        request = urllib.request.Request(
            "%s/metadata/%d" % (origin, requested_id),
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read(MAX_DETAIL_BYTES + 1)
                if len(raw) > MAX_DETAIL_BYTES:
                    raise MetadataUnavailable("metadata detail response is too large")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise MetadataUnavailable("metadata detail request failed") from None
        except MetadataUnavailable:
            raise
        except Exception:
            raise MetadataUnavailable("metadata detail request failed") from None

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MetadataUnavailable("metadata detail response is invalid") from None
        if not isinstance(payload, dict):
            raise MetadataUnavailable("metadata detail response is invalid")

        identities = [_positive_id(payload.get(key)) for key in ("mytv_id", "id")
                      if payload.get(key) is not None]
        if not identities or any(value is None or value != requested_id for value in identities):
            raise MetadataIdentityConflict("metadata detail identity does not match request")
        return payload

    def _validated_origin(self):
        if not self.base_url:
            raise MetadataUnavailable("metadata API origin is not configured")
        try:
            parsed = urllib.parse.urlsplit(self.base_url)
            valid = (
                parsed.scheme in ("http", "https")
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment
                and parsed.port != 0
            )
        except ValueError:
            valid = False
        if not valid:
            raise MetadataUnavailable("metadata API origin is invalid")
        return "%s://%s" % (parsed.scheme, parsed.netloc)

#!/usr/bin/env python3
"""Validate the My TV acceptance ledger and generate a privacy-filtered report."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import zlib
from contextvars import ContextVar
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "tools" / "qa" / "media_matrix.json"
VALID_STATUSES = {"PASS", "FAIL", "NOT-VERIFIED", "N/A"}
URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
AUTH_HEADER_RE = re.compile(r"(?i)\b(?:proxy-)?authorization\s*:\s*[^\r\n]+")
COOKIE_HEADER_RE = re.compile(r"(?i)\b(?:set-)?cookie\s*:\s*[^\r\n]+")
SIGNED_PARAMETER_RE = re.compile(
    r"(?i)([?&](?:token|access_token|sign|signature|sig|auth|key|expires)=)[^&#\s]+"
)
LICENSE_VALUE_RE = re.compile(
    r"(?i)\b(?:license|activation|card)\s*(?:key|code)?\s*[:=]\s*[^\s,;]+"
)
DEVICE_SERIAL_RE = re.compile(r"(?i)\bemulator-\d+\b")
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|cookie|authorization|credential|api[_-]?key|"
    r"signed[_-]?url|playback[_-]?url|private[_-]?key|license[_-]?code|activation[_-]?code)"
)
REDACTED = "[REDACTED]"
REQUIRED_MEDIA_COVERAGE = {
    "movie": 10,
    "series": 10,
    "1080p": 5,
    "low_resolution_legacy_source": 5,
    "hls": 5,
    "direct_link": 5,
}
REQUIRED_NETWORK_PROFILE_IDS = {
    "stable-wifi",
    "high-latency",
    "limit-10mbps",
    "limit-5mbps",
    "packet-loss-1pct",
    "outage-3s",
    "outage-10s",
}
REQUIRED_PLAYBACK_PRODUCTS = {"android_tv", "android_mobile", "web"}
REQUIRED_SAMPLE_DURATIONS = {5, 30}
REQUIRED_ACCEPTANCE_ROW_IDS = {
    *(f"tv-{number:02d}" for number in range(1, 18)),
    "tv-remote-focus", "tv-dlna-na", "tv-gesture-na",
    *(f"mobile-{number:02d}" for number in range(1, 18)),
    "mobile-dlna", "mobile-gesture", "mobile-tv-remote-na",
    *(f"selfhosted-{number:02d}" for number in range(1, 7)),
    "web-all-workflows", "admin-all-workflows", "mobile-rotation",
}
REQUIRED_BROWSER_ROW_IDS = {
    *(f"web-{number:02d}-{view}" for number in range(1, 13) for view in ("desktop", "mobile")),
    *(f"admin-{number:02d}-{view}" for number in (*range(1, 7), *range(8, 16)) for view in ("desktop", "mobile")),
    "admin-import-desktop-53", "admin-import-mobile-54",
    "admin-import-desktop-55", "admin-import-mobile-56",
    "admin-import-desktop-57", "admin-import-mobile-58",
}
REQUIRED_LONGRUN_ROW_IDS = {
    "android_tv-representative-120m",
    "android_mobile-representative-120m",
    "web-representative-120m",
}
REQUIRED_MEDIA_SAMPLE_IDS = {
    *(f"sample-movie-{number:02d}" for number in range(1, 11)),
    *(f"sample-series-{number:02d}" for number in range(1, 11)),
}
REQUIRED_MEDIA_PLAYBACK_ROW_IDS = {
    f"{sample_id}-{product}-{duration}m"
    for sample_id in REQUIRED_MEDIA_SAMPLE_IDS
    for product in REQUIRED_PLAYBACK_PRODUCTS
    for duration in REQUIRED_SAMPLE_DURATIONS
}
REQUIRED_PERFORMANCE_METRIC_IDS = {
    "metadata-api-p95", "business-api-p95", "resolver-fast-path-p95",
    "resolver-first-source-p50", "resolver-match-rate", "http-5xx",
    "tv-cached-home", "tv-catalog-first-screen", "tv-detail-basic-info",
    "tv-first-frame-p50", "tv-first-frame-p95", "tv-30min-interruption-rate",
    "tv-rebuffer-ratio", "tv-auto-switch-recovery", "tv-severe-jank",
    "mobile-cached-home", "mobile-catalog-first-screen", "mobile-detail-basic-info",
    "mobile-first-frame-p50", "mobile-first-frame-p95", "mobile-30min-interruption-rate",
    "mobile-rebuffer-ratio", "mobile-auto-switch-recovery", "mobile-severe-jank",
    "direct-relay-distribution",
}
REQUIRED_SERVICE_ROW_IDS = {"importer-least-privilege", "importer-signature-validation"}
REQUIRED_ROW_IDS = {
    "acceptance_runs": REQUIRED_ACCEPTANCE_ROW_IDS - {"tv-dlna-na", "tv-gesture-na", "mobile-tv-remote-na"},
    "browser_runs": REQUIRED_BROWSER_ROW_IDS,
    "longrun_runs": REQUIRED_LONGRUN_ROW_IDS,
    "media_samples": REQUIRED_MEDIA_SAMPLE_IDS,
    "network_profiles": REQUIRED_NETWORK_PROFILE_IDS,
    "performance_metrics": REQUIRED_PERFORMANCE_METRIC_IDS,
    "service_runs": REQUIRED_SERVICE_ROW_IDS,
}
REQUIRED_DEPLOYMENT_ROW_IDS = {
    "isolated_staging", "vps_egress", "database_backup_restore",
    "cloudbase_runtime_requests", "object_storage_contract",
}
REQUIRED_PRODUCT_DOMAINS = {
    "acceptance_runs": {"android_tv", "android_mobile", "web", "admin", "selfhosted"},
    "browser_runs": {"web", "admin"},
    "longrun_runs": {"android_tv", "android_mobile", "web"},
}
CANONICAL_ROW_IDS = {
    "acceptance_runs": REQUIRED_ACCEPTANCE_ROW_IDS,
    "browser_runs": REQUIRED_BROWSER_ROW_IDS,
    "longrun_runs": REQUIRED_LONGRUN_ROW_IDS,
    "media_samples": REQUIRED_MEDIA_SAMPLE_IDS,
    "network_profiles": REQUIRED_NETWORK_PROFILE_IDS,
    "performance_metrics": REQUIRED_PERFORMANCE_METRIC_IDS,
    "service_runs": REQUIRED_SERVICE_ROW_IDS,
}
REQUIRED_BROWSER_WORKFLOWS = {
    "web": {
        "首页",
        "足球列表/筛选/焦点比赛",
        "片库分类/筛选/加载更多",
        "搜索",
        "详情/剧集",
        "真实媒体播放",
        "直连失败后的受控 relay/重试/换线",
        "本地收藏/历史/进度刷新恢复",
        "IPTV 分组/频道/切台",
        "未授权拦截",
        "隔离测试卡激活/续费/状态刷新",
        "HTML no-store/哈希 JS-CSS 存在且缓存头正确",
    },
    "admin": {
        "登录失败与成功",
        "会话刷新/权限",
        "总览",
        "激活日志筛选",
        "卡密查询/状态/解绑/售出/备注",
        "卡密补货/签发/CSV 下载",
        "热更与 APK 分片上传/版本清单/哈希/下载",
        "公告发布与读取",
        "电视源导入草稿/频道分组/Logo 编辑/探测/发布/版本/回滚/模式切换",
        "影视源发现/草稿/启停/排序/单源测试/全量测速/显式发布",
        "失败发布保留旧版本",
        "隔离 staging 写入后逐项读回",
        "卡密导入脚本最小权限校验",
        "卡密导入签名校验",
        "重复批次幂等和实际插入数与 UI 报告一致",
        "对象上传权限/签名/分片完整性/重复对象处理",
        "对象下载 GET/HEAD/Range/ETag/Last-Modified/缓存头/内容哈希",
    },
}
RUN_RESULT_FIELDS = {
    "status",
    "product_version",
    "build_version",
    "device_or_browser",
    "browser_version",
    "viewport",
    "network_profile",
    "content_category",
    "attempts",
    "first_frame_ms",
    "rebuffer_count",
    "auto_switch_result",
    "survival_minutes",
    "error_log_ref",
    "error_log_result",
    "screenshot_paths",
    "screenshot_reviewed",
    "evidence_ref",
    "staging_environment_ref",
    "readback_status",
    "readback_evidence_ref",
}
_SELF_TEST_ACTIVE: ContextVar[bool] = ContextVar("mytv_acceptance_self_test_active", default=False)
_VIEWPORT_DIMENSIONS_RE = re.compile(r"(?<!\d)(\d{2,5})\s*[x×]\s*(\d{2,5})(?!\d)", re.IGNORECASE)
_SYNTHETIC_PREFIX_RE = re.compile(r"(?i)^\s*(?:fixture|synthetic|testdata)(?:-|:|/|$)")
_SYNTHETIC_EVIDENCE_FIELDS = {
    "evidence_ref", "readback_evidence_ref", "source_ref", "content_ref", "product_version",
    "build_version", "device_or_browser", "browser_version", "network_profile", "path", "row_id",
    "auto_switch_result", "content_category", "registration_evidence_ref", "write_readback_evidence",
}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Canonical browser evidence requirements are pinned in code, not read from the
# ledger. The matrix is the artifact under test, so letting it declare its own
# requirements would let a matrix edit quietly switch a hard gate off.
CANONICAL_BROWSER_SCREENSHOT_ROW_IDS = frozenset(REQUIRED_BROWSER_ROW_IDS)
# Rows that write to isolated staging and must read every write back.
CANONICAL_BROWSER_WRITE_READBACK_ROW_IDS = frozenset({
    *(f"admin-{number:02d}-{view}" for number in (6, 8, 9, 10, 11, 13, 14) for view in ("desktop", "mobile")),
    "admin-import-desktop-53", "admin-import-mobile-54",
    "admin-import-desktop-55", "admin-import-mobile-56",
    "admin-import-desktop-57", "admin-import-mobile-58",
})


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    blockers: list[str]
    status: str


def load_ledger(path: Path | str = DEFAULT_LEDGER) -> dict[str, Any]:
    """Load the acceptance matrix from UTF-8 JSON."""
    ledger_path = Path(path)
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read acceptance ledger: {ledger_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in acceptance ledger: {ledger_path}: {exc}") from exc
    if not isinstance(ledger, dict):
        raise ValueError("acceptance ledger root must be a JSON object")
    if _looks_like_acceptance_ledger(ledger) and _contains_synthetic_data(ledger):
        raise ValueError("fixture or synthetic acceptance data is not importable outside --self-test")
    return ledger


def _looks_like_acceptance_ledger(value: dict[str, Any]) -> bool:
    return any(key in value for key in ("acceptance_runs", "browser_runs", "longrun_runs", "media_samples"))


def _contains_synthetic_data(value: Any, key: str = "") -> bool:
    if isinstance(value, dict):
        if value.get("self_test_mode") is True or value.get("synthetic_data") is True:
            return True
        for child_key, child in value.items():
            normalized_key = str(child_key).lower()
            if normalized_key in {"fixture", "synthetic", "testdata", "fixture_only"} and child not in (False, None, ""):
                return True
            if normalized_key in _SYNTHETIC_EVIDENCE_FIELDS and isinstance(child, str) and _SYNTHETIC_PREFIX_RE.match(child):
                return True
            if _contains_synthetic_data(child, normalized_key):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_synthetic_data(child, key) for child in value)
    return isinstance(value, str) and key in _SYNTHETIC_EVIDENCE_FIELDS and bool(_SYNTHETIC_PREFIX_RE.match(value))


def _check_canonical_rows(ledger: dict[str, Any], blockers: list[str]) -> None:
    all_ids: dict[str, str] = {}
    for collection, expected_ids in CANONICAL_ROW_IDS.items():
        rows = ledger.get(collection)
        if not isinstance(rows, list):
            blockers.append(f"{collection}: canonical required row collection is missing")
            continue
        observed: set[str] = set()
        duplicates: set[str] = set()
        required_flags: dict[str, bool] = {}
        domains: set[str] = set()
        browser_workflows: set[tuple[str, str, str]] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = row.get("id")
            if isinstance(row_id, str) and row_id:
                if row_id in observed:
                    duplicates.add(row_id)
                if row_id in all_ids and all_ids[row_id] != collection:
                    blockers.append(f"{collection}: duplicate row ID {row_id} also appears in {all_ids[row_id]}")
                else:
                    all_ids[row_id] = collection
                observed.add(row_id)
                required_flags[row_id] = row.get("required") is True
            product = row.get("product")
            if isinstance(product, str):
                domains.add(product)
            if collection == "browser_runs" and all(isinstance(row.get(field), str) for field in ("product", "feature", "viewport")):
                key = (row["product"], row["feature"], row["viewport"])
                if key in browser_workflows:
                    blockers.append(f"browser_runs: duplicate workflow domain {key!r}")
                browser_workflows.add(key)
        for duplicate in sorted(duplicates):
            blockers.append(f"{collection}: duplicate row ID {duplicate}")
        for missing in sorted(expected_ids - observed):
            blockers.append(f"{collection}: canonical required row ID {missing} is missing")
        for improperly_optional in sorted(REQUIRED_ROW_IDS.get(collection, set()) & observed):
            if not required_flags.get(improperly_optional, False):
                blockers.append(f"{collection}: canonical required row {improperly_optional} cannot be marked optional")
        if collection == "browser_runs":
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_id = row.get("id")
                if not isinstance(row_id, str) or row_id not in CANONICAL_BROWSER_SCREENSHOT_ROW_IDS:
                    continue
                if row.get("screenshot_required") is not True:
                    blockers.append(
                        f"browser_runs: canonical required row {row_id} cannot disable its required screenshot evidence"
                    )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_id = row.get("id")
                if not isinstance(row_id, str) or row_id not in CANONICAL_BROWSER_WRITE_READBACK_ROW_IDS:
                    continue
                if row.get("write_readback_required") is not True:
                    blockers.append(
                        f"browser_runs: canonical required row {row_id} cannot disable its required write/readback evidence"
                    )
        for domain in sorted(REQUIRED_PRODUCT_DOMAINS.get(collection, set()) - domains):
            blockers.append(f"{collection}: required product domain {domain} is missing")

    media_samples = ledger.get("media_samples")
    if isinstance(media_samples, list):
        observed_playback_ids: set[str] = set()
        duplicate_playback_ids: set[str] = set()
        duplicate_playback_domains: set[tuple[str, str, str]] = set()
        for sample in media_samples:
            if not isinstance(sample, dict) or not isinstance(sample.get("runs"), list):
                continue
            sample_id = sample.get("id")
            seen_domains: set[tuple[str, Any]] = set()
            for run in sample["runs"]:
                if not isinstance(run, dict):
                    continue
                row_id = run.get("id")
                if isinstance(row_id, str) and row_id:
                    if row_id in observed_playback_ids:
                        duplicate_playback_ids.add(row_id)
                    if row_id in all_ids:
                        blockers.append(f"media_samples: duplicate row ID {row_id} also appears in {all_ids[row_id]}")
                    else:
                        all_ids[row_id] = "media_samples playback"
                    observed_playback_ids.add(row_id)
                    if run.get("required") is not True:
                        blockers.append(f"media_samples: canonical required playback row {row_id} cannot be marked optional")
                domain = (str(run.get("product")), str(run.get("duration_minutes")))
                if domain in seen_domains:
                    duplicate_playback_domains.add((str(sample_id), domain[0], domain[1]))
                seen_domains.add(domain)
        for duplicate in sorted(duplicate_playback_ids):
            blockers.append(f"media_samples: duplicate playback row ID {duplicate}")
        for duplicate_sample, duplicate_product, duplicate_duration in sorted(duplicate_playback_domains):
            blockers.append(f"media {duplicate_sample}: duplicate playback domain {duplicate_product} {duplicate_duration}m")
        for missing in sorted(REQUIRED_MEDIA_PLAYBACK_ROW_IDS - observed_playback_ids):
            blockers.append(f"media_samples: canonical required playback row ID {missing} is missing")

    deployment = ledger.get("deployment_evidence")
    if not isinstance(deployment, dict):
        blockers.append("deployment_evidence: canonical required deployment rows are missing")
    else:
        for missing in sorted(REQUIRED_DEPLOYMENT_ROW_IDS - set(deployment)):
            blockers.append(f"deployment_evidence: canonical required row {missing} is missing")
        for required_id in REQUIRED_DEPLOYMENT_ROW_IDS & set(deployment):
            if not isinstance(deployment[required_id], dict) or deployment[required_id].get("required") is not True:
                blockers.append(f"deployment_evidence: canonical required row {required_id} cannot be marked optional")
        for extra in sorted(set(deployment) - REQUIRED_DEPLOYMENT_ROW_IDS):
            if isinstance(deployment[extra], dict) and deployment[extra].get("required"):
                blockers.append(f"deployment_evidence: unknown required deployment row {extra}")


def _local_artifact_path(raw_path: Any, artifact_root: Path) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path.strip() or not _is_safe_reference(raw_path):
        return None
    root = artifact_root.resolve()
    path = _clean_path(raw_path, artifact_root)
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _viewport_dimensions(viewport: Any) -> set[tuple[int, int]]:
    if not isinstance(viewport, str):
        return set()
    return {
        (int(match.group(1)), int(match.group(2)))
        for match in _VIEWPORT_DIMENSIONS_RE.finditer(viewport)
    }


def _png_dimensions(image_path: Path) -> tuple[int, int] | None:
    """Read a PNG's declared size and validate its structure.

    Dimensions come from IHDR, so this deliberately stops before unfiltering
    pixels; callers that need pixel data use `_png_pixel_variety`.
    """
    header = _png_structure(image_path)
    return None if header is None else (header[0], header[1])


def _png_structure(image_path: Path) -> tuple[int, int, int, bytes] | None:
    """Return (width, height, channels, filtered scanline bytes) or None."""
    decoded = _read_png(image_path, unfilter=False)
    return None if decoded is None else (decoded[0], decoded[1], decoded[2], decoded[3])


def _paeth_predictor(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    return above if above_distance <= upper_left_distance else upper_left


def _unfilter_sub(line: bytearray, row_bytes: int, channels: int) -> None:
    """Reverse the Sub filter in place; each byte depends only on the same channel."""
    for index in range(channels, row_bytes):
        line[index] = (line[index] + line[index - channels]) & 0xFF


def _unfilter_up(line: bytearray, previous: bytearray) -> None:
    """Reverse the Up filter; it is a straight per-byte add of the row above."""
    for index in range(len(line)):
        line[index] = (line[index] + previous[index]) & 0xFF


def _unfilter_average(line: bytearray, previous: bytearray, row_bytes: int, channels: int) -> None:
    """Reverse the Average filter."""
    for index in range(row_bytes):
        left = line[index - channels] if index >= channels else 0
        line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF


def _unfilter_paeth(line: bytearray, previous: bytearray, row_bytes: int, channels: int) -> None:
    """Reverse the Paeth filter."""
    for index in range(row_bytes):
        left = line[index - channels] if index >= channels else 0
        upper_left = previous[index - channels] if index >= channels else 0
        line[index] = (line[index] + _paeth_predictor(left, previous[index], upper_left)) & 0xFF


def _read_png(image_path: Path, *, unfilter: bool) -> tuple[int, int, int, bytes] | None:
    """Read a plain 8-bit PNG, optionally unfiltering it into raw pixel bytes.

    Only non-interlaced 8-bit greyscale/RGB/greyscale-alpha/RGBA images are
    accepted, which covers screenshot capture output. Anything else returns None
    so the caller can treat the file as an unreadable image.
    """
    try:
        data = image_path.read_bytes()
    except OSError:
        return None
    if not data.startswith(_PNG_SIGNATURE):
        return None
    offset = len(_PNG_SIGNATURE)
    width = height = bit_depth = color_type = None
    idat = bytearray()
    saw_iend = False
    saw_idat = False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        kind = data[offset:offset + 4]
        offset += 4
        if length > len(data) - offset - 4:
            return None
        payload = data[offset:offset + length]
        offset += length
        recorded_crc = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != recorded_crc:
            return None
        if kind == b"IHDR":
            if width is not None or length != 13:
                return None
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if compression != 0 or filtering != 0 or interlace != 0:
                return None
        elif kind == b"IDAT":
            if width is None or saw_iend:
                return None
            saw_idat = True
            idat.extend(payload)
        elif kind == b"IEND":
            if length != 0 or not saw_idat:
                return None
            saw_iend = True
            break
    if not saw_iend or offset != len(data) or width is None or height is None:
        return None
    if width <= 0 or height <= 0 or width * height > 60_000_000 or bit_depth != 8:
        return None
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        return None
    expected_length = (width * channels + 1) * height
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(bytes(idat), expected_length + 1)
    except zlib.error:
        return None
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or len(decoded) != expected_length:
        return None
    stride = width * channels + 1
    if any(decoded[row * stride] > 4 for row in range(height)):
        return None
    if not unfilter:
        return width, height, channels, bytes(decoded)
    row_bytes = width * channels
    pixels = bytearray(row_bytes * height)
    previous = bytearray(row_bytes)
    for row_index in range(height):
        start = row_index * stride
        filter_type = decoded[start]
        line = bytearray(decoded[start + 1:start + stride])
        if filter_type == 1:
            _unfilter_sub(line, row_bytes, channels)
        elif filter_type == 2:
            _unfilter_up(line, previous)
        elif filter_type == 3:
            _unfilter_average(line, previous, row_bytes, channels)
        elif filter_type == 4:
            _unfilter_paeth(line, previous, row_bytes, channels)
        pixels[row_index * row_bytes:(row_index + 1) * row_bytes] = line
        previous = line
    return width, height, channels, bytes(pixels)


# A screenshot that is one flat colour proves nothing rendered. Real UI captures
# always carry several distinct pixel values, so a tiny palette is the signal
# that a placeholder or blank canvas was submitted instead of the page.
MIN_SCREENSHOT_COLOR_VARIETY = 3
# Sample a grid of this many cells per axis when judging whether a screenshot
# is a flat colour. Bounded work keeps the check cheap on 1440x900 captures.
PNG_SAMPLE_GRID = 32


def _png_pixel_variety(image_path: Path) -> int | None:
    """Count distinct pixel colours in a screenshot, or None if unreadable.

    The image is sampled on a bounded grid rather than scanned pixel by pixel.
    A flat image is flat everywhere, so the grid reaches the same verdict while
    a real capture stops as soon as enough distinct colours are seen.
    """
    decoded = _read_png(image_path, unfilter=True)
    if decoded is None:
        return None
    width, height, channels, pixels = decoded
    # Greyscale-alpha and RGBA: ignore the alpha channel when comparing colours.
    step = channels - 1 if channels in {2, 4} else channels
    colours: set[bytes] = set()
    row_bytes = width * channels
    columns = range(0, width, max(1, width // PNG_SAMPLE_GRID))
    for y in range(0, height, max(1, height // PNG_SAMPLE_GRID)):
        row_start = y * row_bytes
        for x in columns:
            index = row_start + x * channels
            colours.add(pixels[index:index + step])
            if len(colours) >= MIN_SCREENSHOT_COLOR_VARIETY:
                return len(colours)
    return len(colours)


def _screenshot_is_blank(image_path: Path) -> bool:
    """Return True when a screenshot carries too little colour to prove anything rendered."""
    variety = _png_pixel_variety(image_path)
    if variety is None:
        return False
    return variety < MIN_SCREENSHOT_COLOR_VARIETY


def _check_bound_evidence(
    row: dict[str, Any],
    label: str,
    blockers: list[str],
    artifact_root: Path,
    *,
    require_screenshots: bool = False,
    require_measurements: bool = False,
) -> dict[str, Any] | None:
    if _SELF_TEST_ACTIVE.get() and _contains_synthetic_data(row):
        return None
    evidence_path = _local_artifact_path(row.get("evidence_ref"), artifact_root)
    if evidence_path is None or not evidence_path.is_file():
        blockers.append(f"{label}: evidence file does not exist under the artifact root")
        return None
    try:
        manifest = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        blockers.append(f"{label}: evidence file must be valid UTF-8 JSON")
        return None
    if not isinstance(manifest, dict):
        blockers.append(f"{label}: evidence manifest must be a JSON object")
        return None
    if not _SELF_TEST_ACTIVE.get() and _contains_synthetic_data(manifest):
        blockers.append(f"{label}: fixture or synthetic evidence manifest is prohibited")
    for field in ("id", "product_version", "build_version"):
        expected = row.get(field)
        manifest_field = "row_id" if field == "id" else field
        if not expected or manifest.get(manifest_field) != expected:
            blockers.append(f"{label}: evidence does not bind to row/build version ({manifest_field})")
    if row.get("viewport") and manifest.get("viewport") != row.get("viewport"):
        blockers.append(f"{label}: evidence viewport does not bind to the row")

    if require_screenshots:
        expected_paths = row.get("screenshot_paths") if isinstance(row.get("screenshot_paths"), list) else []
        manifest_shots = manifest.get("screenshots")
        if not isinstance(manifest_shots, list):
            blockers.append(f"{label}: evidence manifest is missing screenshot bindings")
            return manifest
        expected_by_path: dict[str, tuple[str, tuple[int, int]]] = {}
        root_resolved = artifact_root.resolve()
        for screenshot in expected_paths:
            screenshot_path = _local_artifact_path(screenshot, artifact_root)
            if screenshot_path is None or not screenshot_path.is_file():
                continue
            dimensions = _png_dimensions(screenshot_path)
            if dimensions:
                expected_by_path[screenshot_path.relative_to(root_resolved).as_posix()] = (
                    hashlib.sha256(screenshot_path.read_bytes()).hexdigest(), dimensions
                )
        manifest_by_path: dict[str, dict[str, Any]] = {}
        for screenshot in manifest_shots:
            if not isinstance(screenshot, dict):
                continue
            path = _local_artifact_path(screenshot.get("path"), artifact_root)
            if path is None:
                continue
            manifest_by_path[path.relative_to(root_resolved).as_posix()] = screenshot
        if set(manifest_by_path) != set(expected_by_path):
            blockers.append(f"{label}: evidence screenshots do not bind to the row's screenshot paths")
        for screenshot_path, (digest, dimensions) in expected_by_path.items():
            item = manifest_by_path.get(screenshot_path, {})
            if item.get("sha256") != digest or item.get("width") != dimensions[0] or item.get("height") != dimensions[1]:
                blockers.append(f"{label}: evidence screenshot hash/dimensions do not bind to the captured image")

    if require_measurements:
        measurements = manifest.get("measurements")
        if not isinstance(measurements, dict):
            blockers.append(f"{label}: evidence manifest is missing measured playback fields")
        else:
            for field in (
                "attempts", "first_frame_ms", "rebuffer_count", "auto_switch_result", "survival_minutes",
                "measurement_started_at", "measurement_ended_at",
            ):
                if measurements.get(field) != row.get(field):
                    blockers.append(f"{label}: evidence measurement {field} does not match the row")
    return manifest


def _check_referenced_synthetic_evidence(ledger: dict[str, Any], artifact_root: Path, blockers: list[str]) -> None:
    if _SELF_TEST_ACTIVE.get():
        return
    row_collections = (
        "acceptance_runs", "browser_runs", "longrun_runs", "network_profiles",
        "performance_metrics", "service_runs",
    )
    rows: list[dict[str, Any]] = []
    for collection in row_collections:
        section = ledger.get(collection)
        if isinstance(section, list):
            rows.extend(row for row in section if isinstance(row, dict))
    samples = ledger.get("media_samples")
    if isinstance(samples, list):
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            rows.append(sample)
            nested = sample.get("runs")
            if isinstance(nested, list):
                rows.extend(row for row in nested if isinstance(row, dict))
    deployment = ledger.get("deployment_evidence")
    if isinstance(deployment, dict):
        rows.extend(row for row in deployment.values() if isinstance(row, dict))
    staging_identity = ledger.get("staging_environment_registry")
    if isinstance(staging_identity, dict):
        rows.append(staging_identity)

    checked: set[Path] = set()
    for row in rows:
        label = row.get("id") or row.get("feature") or "acceptance row"
        for field in ("evidence_ref", "readback_evidence_ref", "registration_evidence_ref", "write_readback_evidence"):
            path = _local_artifact_path(row.get(field), artifact_root)
            if path is None or not path.is_file() or path.suffix.lower() != ".json" or path in checked:
                continue
            checked.add(path)
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if _contains_synthetic_data(manifest):
                blockers.append(f"{label}: referenced {field} contains fixture or synthetic evidence")


def _is_production_reference(value: Any) -> bool:
    return isinstance(value, str) and bool(re.search(r"(?i)(?:^|[/_.:-])prod(?:uction)?(?:$|[/_.:-])", value.strip()))


def _check_staging_identity(ledger: dict[str, Any], blockers: list[str], artifact_root: Path) -> dict[str, Any] | None:
    identity = ledger.get("staging_environment_registry")
    if not isinstance(identity, dict):
        blockers.append("staging identity must be registered as isolated staging; production is prohibited")
        return None
    reference = identity.get("environment_ref")
    if identity.get("registered") is not True or identity.get("status") != "PASS":
        blockers.append("staging identity must be registered and verified before any write acceptance")
    if identity.get("environment_type") != "isolated_staging":
        blockers.append("staging identity must have environment_type=isolated_staging; production is prohibited")
    if not identity.get("id") or not _is_safe_reference(reference) or _is_production_reference(reference):
        blockers.append("staging identity must have a safe non-production environment reference")
    if identity.get("registered") is True and identity.get("status") == "PASS" and not _SELF_TEST_ACTIVE.get():
        registration_path = _local_artifact_path(identity.get("registration_evidence_ref"), artifact_root)
        if registration_path is None or not registration_path.is_file():
            blockers.append("staging identity registration evidence file is missing")
        else:
            try:
                registration = json.loads(registration_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                registration = None
            if isinstance(registration, dict) and _contains_synthetic_data(registration):
                blockers.append("staging identity registration evidence contains fixture or synthetic data")
            if not isinstance(registration, dict) or any(registration.get(field) != expected for field, expected in (
                ("identity_id", identity.get("id")),
                ("environment_type", "isolated_staging"),
                ("environment_ref", reference),
                ("registered", True),
            )):
                blockers.append("staging identity registration evidence does not bind to the registered isolated environment")
    return identity


def redact_sensitive(value: Any, key: str = "") -> Any:
    """Recursively redact credential-bearing fields and common private values."""
    if SENSITIVE_KEY_RE.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(item_key): redact_sensitive(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    if not isinstance(value, str):
        return value

    cleaned = URL_RE.sub("[REDACTED_URL]", value)
    cleaned = AUTH_HEADER_RE.sub("Authorization: [REDACTED]", cleaned)
    cleaned = COOKIE_HEADER_RE.sub("Cookie: [REDACTED]", cleaned)
    cleaned = BEARER_RE.sub("Bearer [REDACTED]", cleaned)
    cleaned = SIGNED_PARAMETER_RE.sub(r"\1[REDACTED]", cleaned)
    cleaned = EMAIL_RE.sub("[REDACTED_EMAIL]", cleaned)
    cleaned = LICENSE_VALUE_RE.sub("[REDACTED_LICENSE]", cleaned)
    cleaned = DEVICE_SERIAL_RE.sub("[REDACTED_DEVICE_ID]", cleaned)
    return cleaned


def _clean_path(raw_path: str | Path, artifact_root: Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = artifact_root / path
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_safe_reference(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "[REDACTED" not in value
        and not URL_RE.search(value)
        and "\n" not in value
        and "\r" not in value
    )


def _metric_passes(actual: Any, threshold: Any) -> bool:
    if not isinstance(threshold, dict):
        return False
    operator = threshold.get("operator")
    expected = threshold.get("value")
    if operator == "recorded_counts":
        fields = expected
        return (
            isinstance(fields, list)
            and len(fields) == 2
            and isinstance(actual, dict)
            and all(_is_number(actual.get(field)) and actual[field] >= 0 for field in fields)
            and sum(actual[field] for field in fields) > 0
        )
    if not _is_number(actual):
        return False
    if operator == "range_inclusive":
        return (
            isinstance(expected, list)
            and len(expected) == 2
            and _is_number(expected[0])
            and _is_number(expected[1])
            and expected[0] <= actual <= expected[1]
        )
    if not _is_number(expected):
        return False
    if operator == "<=":
        return actual <= expected
    if operator == "<":
        return actual < expected
    if operator == ">=":
        return actual >= expected
    if operator == ">":
        return actual > expected
    return False


def _row_status(row: dict[str, Any], label: str, blockers: list[str]) -> str:
    status = row.get("status")
    if status not in VALID_STATUSES:
        blockers.append(f"{label}: invalid or missing status {status!r}")
        return "INVALID"
    if status == "N/A":
        if row.get("required"):
            blockers.append(f"{label}: required row cannot be N/A")
        elif not str(row.get("capability_note") or "").strip():
            blockers.append(f"{label}: N/A requires a product capability note")
    elif row.get("required") and status != "PASS":
        blockers.append(f"{label}: status {status}")
    return status


def _check_run_row(
    row: dict[str, Any],
    label: str,
    blockers: list[str],
    artifact_root: Path,
    *,
    browser_run: bool = False,
    staging_identity: dict[str, Any] | None = None,
    require_measurements: bool = False,
) -> None:
    status = _row_status(row, label, blockers)
    if not row.get("required") or status == "N/A":
        return

    for field in ("product_version", "build_version"):
        if not str(row.get(field) or "").strip():
            blockers.append(f"{label}: missing {field}")

    for field in ("device_or_browser", "network_profile", "content_category"):
        if not str(row.get(field) or "").strip():
            blockers.append(f"{label}: missing {field}")

    if row.get("product") in {"web", "admin"}:
        if not str(row.get("browser_version") or "").strip():
            blockers.append(f"{label}: missing browser_version")
    if browser_run and row.get("viewport") not in {"1440x900", "390x844"}:
        blockers.append(f"{label}: viewport must be 1440x900 or 390x844")

    attempts = row.get("attempts")
    if not _is_number(attempts) or attempts < 1:
        blockers.append(f"{label}: attempts must be at least 1")

    if status != "PASS":
        return

    if row.get("product") == "admin":
        if "隔离 staging" not in str(row.get("data_scope") or ""):
            blockers.append(f"{label}: admin workflow is not constrained to isolated staging")
        _check_row_staging_identity(row, staging_identity, label, blockers)
    if row.get("write_readback_required"):
        if row.get("readback_status") != "PASS":
            blockers.append(f"{label}: staging write readback status is {row.get('readback_status') or 'missing'}")
        _check_row_staging_identity(row, staging_identity, label, blockers)
        if row.get("readback_status") == "PASS":
            readback_row = dict(row)
            readback_row["evidence_ref"] = row.get("readback_evidence_ref")
            _check_readback_manifest(readback_row, label, blockers, artifact_root)

    if not _is_safe_reference(row.get("evidence_ref")):
        blockers.append(f"{label}: missing evidence_ref")
    if not str(row.get("error_log_result") or "").strip() and not _is_safe_reference(row.get("error_log_ref")):
        blockers.append(f"{label}: error log result/reference was not recorded")

    if row.get("screenshot_required"):
        screenshot_paths = row.get("screenshot_paths")
        if not isinstance(screenshot_paths, list) or not screenshot_paths:
            blockers.append(f"{label}: required screenshot is missing")
        else:
            expected_dimensions = _viewport_dimensions(row.get("viewport"))
            if not expected_dimensions:
                blockers.append(f"{label}: viewport must declare screenshot pixel dimensions")
            if expected_dimensions and len(screenshot_paths) != len(expected_dimensions):
                blockers.append(f"{label}: requires one screenshot for each target viewport {sorted(expected_dimensions)}")
            observed_dimensions: set[tuple[int, int]] = set()
            for screenshot in screenshot_paths:
                if not isinstance(screenshot, str) or not screenshot.strip():
                    blockers.append(f"{label}: screenshot path is empty")
                    continue
                screenshot_path = _local_artifact_path(screenshot, artifact_root)
                if screenshot_path is None or not screenshot_path.is_file() or screenshot_path.stat().st_size <= 0:
                    blockers.append(f"{label}: screenshot file does not exist under the artifact root or is empty")
                    continue
                dimensions = _png_dimensions(screenshot_path)
                if dimensions is None:
                    blockers.append(f"{label}: screenshot is not a valid PNG image")
                    continue
                observed_dimensions.add(dimensions)
                if expected_dimensions and dimensions not in expected_dimensions:
                    target = " or ".join(f"{width}x{height}" for width, height in sorted(expected_dimensions))
                    blockers.append(f"{label}: screenshot dimensions {dimensions[0]}x{dimensions[1]} do not match viewport {target}")
                if not _SELF_TEST_ACTIVE.get() and _screenshot_is_blank(screenshot_path):
                    blockers.append(
                        f"{label}: screenshot is blank or single-color and cannot prove the page loaded"
                    )
            if expected_dimensions and observed_dimensions != expected_dimensions:
                blockers.append(f"{label}: screenshots do not cover every target viewport {sorted(expected_dimensions)}")
            if row.get("screenshot_reviewed") is not True:
                blockers.append(f"{label}: screenshot content/viewport review is missing")
        if status == "PASS":
            _check_bound_evidence(
                row,
                label,
                blockers,
                artifact_root,
                require_screenshots=True,
                require_measurements=require_measurements,
            )
    elif status == "PASS":
        _check_bound_evidence(row, label, blockers, artifact_root, require_measurements=require_measurements)


def _check_readback_manifest(
    row: dict[str, Any],
    label: str,
    blockers: list[str],
    artifact_root: Path,
) -> dict[str, Any] | None:
    """Require structured written/read-back pairs for staging write workflows.

    A screenshot shows that something rendered, not that the value written came
    back, so image evidence can never stand in for the readback record.
    """
    readback_label = f"{label} readback"
    readback_path = _local_artifact_path(row.get("evidence_ref"), artifact_root)
    if readback_path is None or not readback_path.is_file():
        blockers.append(f"{readback_label}: structured readback manifest is missing under the artifact root")
        return None
    if readback_path.suffix.lower() != ".json":
        blockers.append(
            f"{readback_label}: readback must be a structured JSON manifest, not {readback_path.suffix or 'a non-JSON file'}"
        )
        return None
    try:
        manifest = json.loads(readback_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        blockers.append(f"{readback_label}: readback manifest must be valid UTF-8 JSON")
        return None
    if not isinstance(manifest, dict):
        blockers.append(f"{readback_label}: readback manifest must be a JSON object")
        return None
    if not _SELF_TEST_ACTIVE.get() and _contains_synthetic_data(manifest):
        blockers.append(f"{readback_label}: fixture or synthetic readback manifest is prohibited")
    expected_staging = row.get("staging_environment_ref")
    if manifest.get("staging_environment_ref") != expected_staging:
        blockers.append(f"{readback_label}: readback manifest does not bind to the staging environment")
    entries = manifest.get("readback_items")
    if not isinstance(entries, list) or not entries:
        blockers.append(f"{readback_label}: readback manifest must list the written items that were read back")
        return manifest
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            blockers.append(f"{readback_label}: readback item {index} is malformed")
            continue
        written = entry.get("written_value")
        read_back = entry.get("read_back_value")
        if written in (None, "", []) or read_back in (None, "", []):
            blockers.append(
                f"{readback_label}: readback item {index} must record both the written and read-back value"
            )
        elif written != read_back:
            blockers.append(
                f"{readback_label}: readback item {index} read-back value does not match what was written"
            )
    return manifest


def _check_row_staging_identity(
    row: dict[str, Any],
    identity: dict[str, Any] | None,
    label: str,
    blockers: list[str],
) -> None:
    reference = row.get("staging_environment_ref")
    if not isinstance(identity, dict) or identity.get("registered") is not True or identity.get("status") != "PASS":
        blockers.append(f"{label}: registered isolated staging identity is required; production is prohibited")
        return
    if identity.get("environment_type") != "isolated_staging":
        blockers.append(f"{label}: staging identity is not isolated staging; production is prohibited")
    if not _is_safe_reference(reference) or reference != identity.get("environment_ref") or _is_production_reference(reference):
        blockers.append(f"{label}: staging environment reference does not match the registered isolated staging identity")


def _measurement_text_is_real(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.strip().lower()
    return not any(token in normalized for token in ("fixture", "synthetic", "pending", "not-verified", "unknown", "n/a"))


def _parse_measurement_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _check_two_hour_measurements(row: dict[str, Any], label: str, blockers: list[str]) -> None:
    survival = row.get("survival_minutes")
    if not _is_number(survival) or survival < 120:
        blockers.append(f"{label}: survival_minutes must be measured at 120 minutes or more")
    first_frame = row.get("first_frame_ms")
    if not _is_number(first_frame) or first_frame <= 0:
        blockers.append(f"{label}: first_frame_ms must be a positive measured value")
    rebuffer_count = row.get("rebuffer_count")
    if not _is_number(rebuffer_count) or rebuffer_count < 0:
        blockers.append(f"{label}: rebuffer_count must be a recorded non-negative value")
    if not _measurement_text_is_real(row.get("auto_switch_result")):
        blockers.append(f"{label}: auto_switch_result must contain a real measured outcome")
    started = _parse_measurement_time(row.get("measurement_started_at"))
    ended = _parse_measurement_time(row.get("measurement_ended_at"))
    if started is None or ended is None or ended < started:
        blockers.append(f"{label}: measurement timestamps are missing or invalid for the 120-minute run")
        return
    elapsed_minutes = (ended - started).total_seconds() / 60
    if elapsed_minutes < 120:
        blockers.append(f"{label}: measured playback window is shorter than 120 minutes")
    if _is_number(survival) and survival > elapsed_minutes + 1:
        blockers.append(f"{label}: survival_minutes exceeds the measured playback window")
    # A window that ends in the future has not been measured yet, so it cannot
    # stand in for a completed two-hour stability run.
    now = datetime.now(timezone.utc)
    if ended > now:
        blockers.append(f"{label}: measurement window ends in the future and cannot be a completed two-hour run")
    if started > now:
        blockers.append(f"{label}: measurement window starts in the future and cannot be a completed two-hour run")


def _eligible_sample(sample: dict[str, Any]) -> bool:
    if sample.get("status") != "PASS" or sample.get("authorized_test") is not True:
        return False
    if not _is_safe_reference(sample.get("content_ref")) or not _is_safe_reference(sample.get("source_ref")):
        return False
    if sample.get("kind") not in {"movie", "series"}:
        return False
    if sample.get("stream_type") not in {"hls", "direct", "direct_link"}:
        return False
    if not _is_number(sample.get("resolution")):
        return False
    runs = sample.get("runs")
    if not isinstance(runs, list):
        return False
    for product in REQUIRED_PLAYBACK_PRODUCTS:
        for duration in REQUIRED_SAMPLE_DURATIONS:
            run = next((item for item in runs if item.get("product") == product and item.get("duration_minutes") == duration), None)
            if not run or run.get("status") != "PASS" or not _is_safe_reference(run.get("evidence_ref")):
                return False
            if not str(run.get("product_version") or "").strip() or not str(run.get("build_version") or "").strip():
                return False
            if not str(run.get("device_or_browser") or "").strip() or not str(run.get("network_profile") or "").strip():
                return False
            if run.get("product") == "web" and not str(run.get("browser_version") or "").strip():
                return False
            if not str(run.get("viewport") or "").strip() or run.get("content_category") != sample.get("kind"):
                return False
            if not _is_number(run.get("attempts")) or run["attempts"] < 1:
                return False
            if not _is_number(run.get("first_frame_ms")) or run["first_frame_ms"] <= 0 or not _is_number(run.get("rebuffer_count")) or run["rebuffer_count"] < 0:
                return False
            if not _measurement_text_is_real(run.get("auto_switch_result")):
                return False
            if not _is_number(run.get("survival_minutes")) or run["survival_minutes"] < duration:
                return False
            if not str(run.get("error_log_result") or "").strip() and not _is_safe_reference(run.get("error_log_ref")):
                return False
            if not run.get("screenshot_paths") or run.get("screenshot_reviewed") is not True:
                return False
    return True


def _check_media(ledger: dict[str, Any], blockers: list[str], artifact_root: Path) -> None:
    samples = ledger.get("media_samples")
    targets = ledger.get("media_coverage_targets")
    if not isinstance(samples, list) or not isinstance(targets, dict):
        blockers.append("media: sample inventory or coverage targets are missing")
        return
    if targets != REQUIRED_MEDIA_COVERAGE:
        blockers.append(
            f"media coverage targets must remain {REQUIRED_MEDIA_COVERAGE}; observed {targets}"
        )

    eligible = []
    for sample in samples:
        label = f"media {sample.get('id', '<missing id>')}"
        status = _row_status(sample, label, blockers)
        if sample.get("required") and status == "PASS":
            for field in ("content_ref", "source_ref", "resolution", "stream_type", "device_or_browser", "product_version", "build_version", "network_profile", "evidence_ref"):
                if sample.get(field) in (None, "", []):
                    blockers.append(f"{label}: missing {field}")
            for field in ("content_ref", "source_ref", "evidence_ref"):
                if not _is_safe_reference(sample.get(field)):
                    blockers.append(f"{label}: {field} must be a safe opaque/local reference")
            if sample.get("authorized_test") is not True:
                blockers.append(f"{label}: authorization evidence is not true")
        runs = sample.get("runs")
        if not isinstance(runs, list):
            blockers.append(f"{label}: per-product 5m/30m playback rows are missing")
            runs = []
        observed_pairs = {
            (run.get("product"), run.get("duration_minutes"))
            for run in runs
            if isinstance(run, dict)
        }
        required_pairs = {
            (product, duration)
            for product in REQUIRED_PLAYBACK_PRODUCTS
            for duration in REQUIRED_SAMPLE_DURATIONS
        }
        for product, duration in sorted(required_pairs - observed_pairs):
            blockers.append(f"{label}: required {product} {duration}m playback row is missing")
        for run in runs:
            if not isinstance(run, dict):
                blockers.append(f"{label}: malformed playback result")
                continue
            run_label = f"{label} {run.get('product')} {run.get('duration_minutes')}m"
            _row_status(run, run_label, blockers)
            if run.get("product") not in REQUIRED_PLAYBACK_PRODUCTS or run.get("duration_minutes") not in REQUIRED_SAMPLE_DURATIONS:
                blockers.append(f"{run_label}: unsupported playback product/duration")
                continue
            if run.get("required"):
                _check_run_row(run, run_label, blockers, artifact_root)
                if run.get("status") == "PASS":
                    if run.get("content_category") != sample.get("kind"):
                        blockers.append(f"{run_label}: content_category does not match sample kind")
                    if not _is_number(run.get("first_frame_ms")) or run["first_frame_ms"] < 0:
                        blockers.append(f"{run_label}: first_frame_ms is missing or invalid")
                    if not _is_number(run.get("rebuffer_count")) or run["rebuffer_count"] < 0:
                        blockers.append(f"{run_label}: rebuffer_count is missing or invalid")
                    if not str(run.get("auto_switch_result") or "").strip():
                        blockers.append(f"{run_label}: auto_switch_result is missing")
                    if not _is_number(run.get("survival_minutes")) or run["survival_minutes"] < run["duration_minutes"]:
                        blockers.append(f"{run_label}: survival time is shorter than the required playback duration")
        if _eligible_sample(sample):
            eligible.append(sample)

    counts = {
        "movie": sum(sample.get("kind") == "movie" for sample in eligible),
        "series": sum(sample.get("kind") == "series" for sample in eligible),
        "1080p": sum(sample.get("resolution") == 1080 for sample in eligible),
        "low_resolution_legacy_source": sum(
            sample.get("source_age") == "legacy"
            and _is_number(sample.get("resolution"))
            and sample.get("resolution") < 1080
            for sample in eligible
        ),
        "hls": sum(sample.get("stream_type") == "hls" for sample in eligible),
        "direct_link": sum(sample.get("stream_type") in {"direct", "direct_link"} for sample in eligible),
    }
    for category, required_count in targets.items():
        observed = counts.get(category, 0)
        if observed < required_count:
            blockers.append(f"media coverage {category}: {observed}/{required_count} verified samples")


def _check_network(ledger: dict[str, Any], blockers: list[str]) -> None:
    profiles = ledger.get("network_profiles")
    if not isinstance(profiles, list) or not profiles:
        blockers.append("network: required network profile matrix is missing")
        return
    observed_ids = {profile.get("id") for profile in profiles if isinstance(profile, dict)}
    for missing_id in sorted(REQUIRED_NETWORK_PROFILE_IDS - observed_ids):
        blockers.append(f"network: required profile {missing_id} is missing")
    for profile in profiles:
        if not isinstance(profile, dict):
            blockers.append("network: malformed profile row")
            continue
        label = f"network {profile.get('name') or profile.get('id') or '<missing id>'}"
        status = _row_status(profile, label, blockers)
        if profile.get("required") and status == "PASS":
            for field in ("actual_device_or_proxy", "actual_parameters", "evidence_ref", "device_or_browser"):
                if profile.get(field) in (None, "", []):
                    blockers.append(f"{label}: missing {field}")
            if not _is_safe_reference(profile.get("evidence_ref")):
                blockers.append(f"{label}: evidence_ref must be a safe local reference")


def _check_metrics(ledger: dict[str, Any], blockers: list[str]) -> None:
    metrics = ledger.get("performance_metrics")
    if not isinstance(metrics, list) or not metrics:
        blockers.append("performance: required metrics are missing")
        return
    for item in metrics:
        label = f"performance {item.get('metric') or item.get('id') or '<missing id>'}"
        status = _row_status(item, label, blockers)
        if not item.get("required") or status == "N/A":
            continue
        for field in ("product_version", "build_version", "device_or_browser", "evidence_ref"):
            if not str(item.get(field) or "").strip():
                blockers.append(f"{label}: missing {field}")
        if not _is_safe_reference(item.get("evidence_ref")):
            blockers.append(f"{label}: evidence_ref must be a safe local reference")
        sample_count = item.get("sample_count")
        if not _is_number(sample_count) or sample_count < 1:
            blockers.append(f"{label}: sample_count must be at least 1")
        actual = item.get("actual")
        if status == "PASS":
            threshold = item.get("threshold")
            if not _metric_passes(actual, threshold):
                if isinstance(threshold, dict) and threshold.get("operator") == "recorded_counts":
                    blockers.append(f"{label}: direct/relay counts are missing or invalid")
                else:
                    blockers.append(f"{label}: actual {actual!r} violates or lacks threshold {threshold!r}")
            elif isinstance(threshold, dict) and threshold.get("operator") == "recorded_counts" and sum(actual[field] for field in threshold["value"]) != sample_count:
                blockers.append(f"{label}: direct/relay counts do not equal sample_count")


def _check_services(
    ledger: dict[str, Any],
    blockers: list[str],
    artifact_root: Path,
    staging_identity: dict[str, Any] | None,
) -> None:
    rows = ledger.get("service_runs")
    if not isinstance(rows, list):
        blockers.append("service_runs: required importer security service rows are missing")
        return
    for row in rows:
        if not isinstance(row, dict):
            blockers.append("service_runs: malformed service row")
            continue
        label = f"service {row.get('id', '<missing id>')}"
        status = _row_status(row, label, blockers)
        if not row.get("required") or status != "PASS":
            continue
        for field in ("product_version", "build_version"):
            if not str(row.get(field) or "").strip():
                blockers.append(f"{label}: missing {field}")
        if not _is_number(row.get("attempts")) or row["attempts"] < 1:
            blockers.append(f"{label}: attempts must be at least 1")
        _check_row_staging_identity(row, staging_identity, label, blockers)
        manifest = _check_bound_evidence(row, label, blockers, artifact_root)
        checks = row.get("checks")
        if not isinstance(checks, dict):
            blockers.append(f"{label}: required service security checks are missing")
            continue
        required_checks = {
            "importer-least-privilege": {
                "required_insert_allowed", "privileged_read_denied", "unauthorized_write_denied", "credential_scope_reviewed",
            },
            "importer-signature-validation": {
                "valid_signature_accepted", "tampered_signature_rejected", "payload_tamper_rejected", "rejected_batch_mutations",
            },
        }.get(row.get("id"), set())
        for check in sorted(required_checks):
            value = checks.get(check)
            if check == "rejected_batch_mutations":
                if not _is_number(value) or value != 0:
                    blockers.append(f"{label}: {check} must be a measured zero")
            elif value is not True:
                blockers.append(f"{label}: {check} must be verified true")
        if manifest is not None:
            expected_environment = row.get("staging_environment_ref")
            if manifest.get("environment_ref") != expected_environment:
                blockers.append(f"{label}: evidence does not bind to the staging environment")


# Each deployment gate must bind a manifest whose entry keys prove the claim.
# A gate cannot simply assert PASS: the manifest has to enumerate the same
# evidence the gate already demands, so an empty or status-only file is not a
# pass. CloudBase is included because "zero requests" is a measured claim.
REQUIRED_DEPLOYMENT_MANIFEST_ENTRIES = {
    "isolated_staging": ("environment", "readback"),
    "vps_egress": ("sources", "targets"),
    "database_backup_restore": ("backup", "restore"),
    "cloudbase_runtime_requests": ("observations",),
    "object_storage_contract": ("contract_checks",),
}


def _check_deployment_manifest(
    evidence_id: str,
    item: dict[str, Any],
    label: str,
    blockers: list[str],
    artifact_root: Path | None,
) -> dict[str, Any] | None:
    """Verify the gate's bound manifest exists and substantiates a PASS claim."""
    if artifact_root is None or _SELF_TEST_ACTIVE.get():
        return None
    manifest_path = _local_artifact_path(item.get("evidence_ref"), artifact_root)
    if manifest_path is None or not manifest_path.is_file():
        blockers.append(f"{label}: PASS requires a bound verifiable manifest; evidence_ref is missing")
        return None
    if manifest_path.suffix.lower() != ".json":
        blockers.append(f"{label}: PASS requires a bound verifiable manifest; evidence_ref is not a JSON manifest")
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        blockers.append(f"{label}: PASS requires a bound verifiable manifest; evidence_ref is not valid UTF-8 JSON")
        return None
    if not isinstance(manifest, dict):
        blockers.append(f"{label}: PASS requires a bound verifiable manifest; manifest must be a JSON object")
        return None
    if _contains_synthetic_data(manifest):
        blockers.append(f"{label}: deployment manifest contains fixture or synthetic evidence")
        return None
    entries = manifest.get("manifest_entries")
    if not isinstance(entries, dict) or not entries:
        blockers.append(f"{label}: PASS requires a bound verifiable manifest; manifest_entries are missing or empty")
        return manifest
    for key in REQUIRED_DEPLOYMENT_MANIFEST_ENTRIES.get(evidence_id, ()):
        value = entries.get(key)
        if value in (None, "", [], {}):
            blockers.append(f"{label}: manifest is missing required entry {key!r} for this gate")
    return manifest


def _check_deployment(
    ledger: dict[str, Any],
    blockers: list[str],
    staging_identity: dict[str, Any] | None = None,
    artifact_root: Path | None = None,
) -> None:
    # Accept either the full ledger or the deployment_evidence section itself so
    # both the gate pipeline and direct section checks validate the same rows.
    nested = ledger.get("deployment_evidence")
    evidence = nested if isinstance(nested, dict) else ledger
    if not isinstance(evidence, dict):
        blockers.append("deployment: required deployment evidence is missing")
        return
    for evidence_id, item in evidence.items():
        if not isinstance(item, dict):
            blockers.append(f"deployment {evidence_id}: malformed evidence record")
            continue
        label = f"deployment {evidence_id}"
        status = _row_status(item, label, blockers)
        if item.get("required") and status == "PASS":
            for field in ("evidence_ref",):
                if not _is_safe_reference(item.get(field)):
                    blockers.append(f"{label}: missing {field}")
            _check_deployment_manifest(evidence_id, item, label, blockers, artifact_root)
            if evidence_id == "isolated_staging":
                if item.get("environment_type") != "isolated_staging":
                    blockers.append(f"{label}: environment is not confirmed as isolated staging")
                if not item.get("environment_ref") or not item.get("write_readback_evidence"):
                    blockers.append(f"{label}: staging write/readback evidence is missing")
                if (
                    not isinstance(staging_identity, dict)
                    or item.get("environment_id") != staging_identity.get("id")
                    or item.get("environment_ref") != staging_identity.get("environment_ref")
                    or item.get("environment_type") != "isolated_staging"
                    or _is_production_reference(item.get("environment_ref"))
                ):
                    blockers.append(f"{label}: deployment identity does not match registered isolated staging")
                if artifact_root is not None and not _SELF_TEST_ACTIVE.get():
                    readback_path = _local_artifact_path(item.get("write_readback_evidence"), artifact_root)
                    if readback_path is None or not readback_path.is_file():
                        blockers.append(f"{label}: staging write/readback evidence file is missing")
                    else:
                        try:
                            readback = json.loads(readback_path.read_text(encoding="utf-8"))
                        except (OSError, UnicodeError, json.JSONDecodeError):
                            readback = None
                        if isinstance(readback, dict) and _contains_synthetic_data(readback):
                            blockers.append(f"{label}: fixture or synthetic staging readback evidence is prohibited")
                        if not isinstance(readback, dict) or any(readback.get(field) != expected for field, expected in (
                            ("environment_id", item.get("environment_id")),
                            ("environment_ref", item.get("environment_ref")),
                            ("environment_type", "isolated_staging"),
                            ("readback_status", "PASS"),
                        )):
                            blockers.append(f"{label}: readback evidence does not bind to isolated staging")
            elif evidence_id == "vps_egress":
                if not item.get("enabled_source_inventory_ref") or not item.get("sources_tested"):
                    blockers.append(f"{label}: enabled-source inventory or tested-source list is missing")
                if not item.get("target_categories"):
                    blockers.append(f"{label}: source API/CDN/HLS targets are missing")
            elif evidence_id == "database_backup_restore":
                if not item.get("backup_ref") or not item.get("restore_target_ref"):
                    blockers.append(f"{label}: backup or restore target evidence is missing")
            elif evidence_id == "object_storage_contract":
                if not item.get("checks"):
                    blockers.append(f"{label}: object contract checks are missing")
            elif evidence_id == "cloudbase_runtime_requests":
                observed = item.get("observed_requests")
                if observed is None:
                    blockers.append(f"{label}: request count is NOT-VERIFIED")
                elif observed != 0:
                    blockers.append(f"CloudBase runtime requests must be zero; observed {observed}")
                if not item.get("observation_window"):
                    blockers.append(f"{label}: observation window is missing")


def _check_browser_coverage(ledger: dict[str, Any], blockers: list[str]) -> None:
    rows = ledger.get("browser_runs")
    if not isinstance(rows, list):
        return
    required_viewports = {"1440x900", "390x844"}
    for product in ("web", "admin"):
        product_rows = [row for row in rows if isinstance(row, dict) and row.get("product") == product and row.get("required")]
        if not product_rows:
            blockers.append(f"{product}: required browser viewport matrix is missing")
            continue
        for row in product_rows:
            label = f"{product} {row.get('feature')} @ {row.get('viewport')}"
            if row.get("execution_method") != "manual_browser_e2e":
                blockers.append(f"{label}: execution method must be manual_browser_e2e")
            if not row.get("manual_steps") or not row.get("expected_results"):
                blockers.append(f"{label}: manual browser steps or expected results are missing")
        for viewport in required_viewports:
            features = {row.get("feature") for row in product_rows if row.get("viewport") == viewport}
            if not features:
                blockers.append(f"{product}: no required workflow at viewport {viewport}")
        desktop_features = {row.get("feature") for row in product_rows if row.get("viewport") == "1440x900"}
        mobile_features = {row.get("feature") for row in product_rows if row.get("viewport") == "390x844"}
        expected_features = REQUIRED_BROWSER_WORKFLOWS[product]
        for missing_feature in sorted(expected_features - desktop_features):
            blockers.append(f"{product}: required browser workflow {missing_feature} missing at 1440x900")
        for missing_feature in sorted(expected_features - mobile_features):
            blockers.append(f"{product}: required browser workflow {missing_feature} missing at 390x844")
        missing_mobile = sorted(desktop_features - mobile_features)
        missing_desktop = sorted(mobile_features - desktop_features)
        if missing_mobile:
            blockers.append(f"{product}: workflows missing at 390x844: {', '.join(map(str, missing_mobile))}")
        if missing_desktop:
            blockers.append(f"{product}: workflows missing at 1440x900: {', '.join(map(str, missing_desktop))}")


def evaluate_gate(ledger: dict[str, Any], artifact_root: Path | str | None = None) -> GateResult:
    """Return a release decision; every required row and evidence gate must pass."""
    blockers: list[str] = []
    root = Path(artifact_root) if artifact_root is not None else ROOT
    if _looks_like_acceptance_ledger(ledger) and _contains_synthetic_data(ledger) and not _SELF_TEST_ACTIVE.get():
        blockers.append("fixture or synthetic evidence is restricted to non-persistable --self-test mode")
    _check_referenced_synthetic_evidence(ledger, root, blockers)
    _check_canonical_rows(ledger, blockers)
    staging_identity = _check_staging_identity(ledger, blockers, root)
    for collection in ("acceptance_runs", "browser_runs"):
        rows = ledger.get(collection)
        if not isinstance(rows, list) or not rows:
            blockers.append(f"{collection}: required acceptance rows are missing")
            continue
        for row in rows:
            if not isinstance(row, dict):
                blockers.append(f"{collection}: malformed acceptance row")
                continue
            label = f"{row.get('product', collection)} {row.get('feature', row.get('id', '<missing id>'))}"
            _check_run_row(
                row,
                label,
                blockers,
                root,
                browser_run=collection == "browser_runs",
                staging_identity=staging_identity,
            )
    longrun_rows = ledger.get("longrun_runs")
    if not isinstance(longrun_rows, list) or not longrun_rows:
        blockers.append("longrun_runs: representative two-hour playback rows are missing")
    else:
        samples = ledger.get("media_samples")
        samples_by_id = {
            sample.get("id"): sample
            for sample in samples
            if isinstance(sample, dict) and sample.get("id")
        } if isinstance(samples, list) else {}
        for row in longrun_rows:
            if not isinstance(row, dict):
                blockers.append("longrun_runs: malformed acceptance row")
                continue
            label = f"{row.get('product', 'longrun')} {row.get('feature', row.get('id', '<missing id>'))}"
            if row.get("required") and row.get("duration_minutes") != 120:
                blockers.append(f"{label}: required representative run must be 120 minutes")
            if row.get("required"):
                sample_id = row.get("sample_id")
                sample = samples_by_id.get(sample_id)
                if not isinstance(sample_id, str) or not sample_id.strip():
                    blockers.append(f"{label}: sample_id must reference a completed real-media sample")
                elif not isinstance(sample, dict) or not _eligible_sample(sample):
                    blockers.append(f"{label}: sample_id {sample_id!r} does not reference a completed real-media sample")
                elif row.get("content_category") != sample.get("kind"):
                    blockers.append(f"{label}: content_category must match sample_id {sample_id!r}")
            _check_run_row(
                row,
                label,
                blockers,
                root,
                staging_identity=staging_identity,
                require_measurements=row.get("duration_minutes") == 120,
            )
            if row.get("required") and row.get("status") == "PASS" and row.get("duration_minutes") == 120:
                _check_two_hour_measurements(row, label, blockers)
    _check_browser_coverage(ledger, blockers)
    _check_media(ledger, blockers, root)
    _check_network(ledger, blockers)
    _check_metrics(ledger, blockers)
    _check_services(ledger, blockers, root, staging_identity)
    _check_deployment(ledger, blockers, staging_identity, root)

    # A missing or malformed section is itself a blocker, not an implicit pass.
    if not isinstance(ledger.get("media_coverage_targets"), dict):
        blockers.append("media: coverage target declarations are missing")
    scope = ledger.get("scope")
    if not isinstance(scope, dict) or scope.get("legacy_apk") != "旧 APK 不在本次支持范围。":
        blockers.append("scope: legacy APK support boundary is missing or changed")

    blockers = list(dict.fromkeys(blockers))
    if not blockers:
        return GateResult(allowed=True, blockers=[], status="PASS")
    has_failure = _has_current_failure(ledger)
    cloudbase = ledger.get("deployment_evidence", {}).get("cloudbase_runtime_requests", {})
    if isinstance(cloudbase, dict) and isinstance(cloudbase.get("observed_requests"), (int, float)) and cloudbase["observed_requests"] > 0:
        has_failure = True
    return GateResult(allowed=False, blockers=blockers, status="FAIL" if has_failure else "NOT-VERIFIED")


def _has_current_failure(ledger: dict[str, Any]) -> bool:
    for section in (
        "acceptance_runs", "browser_runs", "longrun_runs", "media_samples",
        "network_profiles", "performance_metrics", "service_runs",
    ):
        rows = ledger.get(section)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("status") == "FAIL":
                return True
            if section == "media_samples" and isinstance(row, dict):
                nested = row.get("runs")
                if isinstance(nested, list) and any(isinstance(run, dict) and run.get("status") == "FAIL" for run in nested):
                    return True
    deployment = ledger.get("deployment_evidence")
    return isinstance(deployment, dict) and any(
        isinstance(row, dict) and row.get("status") == "FAIL"
        for row in deployment.values()
    )


def _md(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    return text


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(_md(cell) for cell in row) + " |" for row in rows)
    return lines


def render_report(
    ledger: dict[str, Any],
    gate: GateResult | None = None,
    artifact_root: Path | str | None = None,
) -> str:
    """Render a traceable report after redacting the complete input tree."""
    if _looks_like_acceptance_ledger(ledger) and _contains_synthetic_data(ledger):
        raise ValueError("fixture or synthetic acceptance data cannot be included in a normal report")
    root = Path(artifact_root) if artifact_root is not None else ROOT
    synthetic_reference_blockers: list[str] = []
    _check_referenced_synthetic_evidence(ledger, root, synthetic_reference_blockers)
    if synthetic_reference_blockers:
        raise ValueError("fixture or synthetic evidence cannot be included in a normal report")
    clean = redact_sensitive(ledger)
    gate = gate or evaluate_gate(clean, artifact_root=root)
    total_required = sum(
        1
        for section in ("acceptance_runs", "browser_runs", "longrun_runs", "media_samples", "network_profiles", "performance_metrics", "service_runs")
        for row in clean.get(section, [])
        if row.get("required")
    )
    status_line = "PASS — all required acceptance rows passed" if gate.allowed else f"{gate.status} — 发布门禁阻断"
    lines = [
        "# My TV 全链路性能与功能验收记录",
        "",
        f"日期：{clean.get('record_date', '未填写')}  ",
        f"状态：**{status_line}**  ",
        f"必测记录：{total_required} 项；阻断项：{len(gate.blockers)} 项  ",
        "当前仅记录当前支持版本；旧 APK 不在本次支持范围。CloudBase 视为停用，运行时请求必须为零。",
        "本 Task 4 未执行真实 Web/后台浏览器测试。所有未提供实测证据的条目保持 NOT-VERIFIED。",
        "",
        "## 发布门禁",
        "",
        f"- 允许发布：{'是' if gate.allowed else '否'}",
        f"- 门禁状态：`{gate.status}`",
        "- 必测项必须逐条为 PASS，并有版本、构建、环境、运行记录和要求的截图/性能证据。",
        "- 缺失值不能记作 N/A；N/A 只允许用于非必测产品能力，并附能力说明。",
        "- 所有管理后台写入、发布、导入和回滚只能在隔离 staging 执行，并经 UI/API 读回。",
        "",
        "## 当前版本功能矩阵",
        "",
    ]
    platform_rows = [
        [row.get("product"), row.get("feature"), row.get("status"), row.get("product_version"), row.get("build_version"), row.get("device_or_browser"), row.get("viewport"), row.get("evidence_ref")]
        for row in clean.get("acceptance_runs", [])
    ]
    lines.extend(_markdown_table(
        ["产品", "功能", "状态", "产品版本", "构建版本", "设备/浏览器", "方向/视口", "证据引用"],
        platform_rows,
    ))
    lines.extend(["", "## Web 与后台浏览器矩阵", "", "每个流程均须在桌面 1440×900 和手机 390×844 两种视口分别验收并截图。"])
    lines.extend([
        "",
        "真实浏览器执行方式（命令只提供步骤/记录结果，不会自行驱动浏览器，也不会自动判 PASS）：",
        "",
        "```powershell",
        "python tools/qa/run_acceptance.py --show-steps <browser_run_id>",
        "python tools/qa/run_acceptance.py --record-result <result.json> --ledger tools/qa/media_matrix.json",
        "```",
        "",
        "先按每条 JSON 行中的 `manual_steps` 和 `expected_results` 在 Chrome staging 实测，再保存结构化版本/视口/网络/首帧/缓冲/截图/错误日志结果。PASS 必须有有效 PNG、逐视口尺寸、人工内容检查、当前产品/构建/浏览器版本及本地 JSON 证据清单；清单需绑定 row ID、版本、构建、截图路径、SHA-256 与尺寸。后台写操作另外需要已登记的 isolated-staging 身份引用和逐项读回证据；production 引用会被拒绝。结果导入不会解除其他未验证行的门禁。",
        "",
    ])
    browser_rows = [
        [row.get("product"), row.get("feature"), row.get("viewport"), row.get("status"), row.get("product_version"), row.get("build_version"), row.get("staging_environment_ref"), row.get("readback_status"), ", ".join(row.get("screenshot_paths") or []), row.get("evidence_ref")]
        for row in clean.get("browser_runs", [])
    ]
    lines.extend(_markdown_table(
        ["产品", "页面/操作", "视口", "状态", "产品版本", "构建版本", "Staging", "写后读回", "截图路径", "执行证据"],
        browser_rows,
    ))

    lines.extend(["", "## 代表性 2 小时连续播放", ""])
    longrun_rows = [
        [row.get("product"), row.get("duration_minutes"), row.get("status"), row.get("product_version"), row.get("device_or_browser"), row.get("network_profile"), row.get("survival_minutes"), row.get("error_log_ref"), ", ".join(row.get("screenshot_paths") or []), row.get("evidence_ref")]
        for row in clean.get("longrun_runs", [])
    ]
    lines.extend(_markdown_table(["产品", "目标分钟", "状态", "版本", "设备/浏览器", "网络", "存活分钟", "错误日志", "截图", "证据"], longrun_rows))

    lines.extend(["", "## 真实媒体样本覆盖", ""])
    targets = clean.get("media_coverage_targets", {})
    verified_samples = [sample for sample in clean.get("media_samples", []) if _eligible_sample(sample)]
    counts = {
        "movie": sum(sample.get("kind") == "movie" for sample in verified_samples),
        "series": sum(sample.get("kind") == "series" for sample in verified_samples),
        "1080p": sum(sample.get("resolution") == 1080 for sample in verified_samples),
        "low_resolution_legacy_source": sum(sample.get("source_age") == "legacy" and sample.get("resolution", 1080) < 1080 for sample in verified_samples),
        "hls": sum(sample.get("stream_type") == "hls" for sample in verified_samples),
        "direct_link": sum(sample.get("stream_type") in {"direct", "direct_link"} for sample in verified_samples),
    }
    coverage_rows = [[name, counts.get(name, 0), required] for name, required in targets.items()]
    lines.extend(_markdown_table(["类别", "已验证", "最低目标"], coverage_rows))
    lines.extend(["", "真实媒体样本均须使用授权来源与不透明脱敏引用；每条样本分别记录 5 分钟、30 分钟结果。"])
    sample_rows = [
        [sample.get("id"), sample.get("kind"), sample.get("stream_type"), sample.get("resolution"), sample.get("episode"), sample.get("authorized_test"), sample.get("status"), sample.get("content_ref"), sample.get("source_ref"), "; ".join(f"{run.get('duration_minutes')}m={run.get('status')}" for run in sample.get("runs", []))]
        for sample in clean.get("media_samples", [])
    ]
    lines.extend(_markdown_table(
        ["样本槽", "类型", "流类型", "分辨率", "Episode", "已授权", "状态", "脱敏内容引用", "脱敏来源", "连续播放"],
        sample_rows,
    ))

    lines.extend(["", "## 网络条件", ""])
    network_rows = [
        [profile.get("name"), profile.get("target_parameters"), profile.get("actual_device_or_proxy"), profile.get("actual_parameters"), profile.get("status"), profile.get("evidence_ref")]
        for profile in clean.get("network_profiles", [])
    ]
    lines.extend(_markdown_table(["网络条件", "目标方法/参数", "实测设备或代理", "实测参数", "状态", "证据"], network_rows))
    lines.extend(["", "实际设备/代理和实测网络参数均须回填；占位方法不构成通过证据。"])

    lines.extend(["", "## 性能门槛", ""])
    metric_rows = [
        [item.get("product"), item.get("metric"), item.get("threshold"), item.get("actual"), item.get("sample_count"), item.get("status"), item.get("evidence_ref")]
        for item in clean.get("performance_metrics", [])
    ]
    lines.extend(_markdown_table(["产品", "指标", "目标", "实测", "样本数", "状态", "证据"], metric_rows))

    lines.extend(["", "## 导入安全服务验证", ""])
    service_rows = [
        [row.get("id"), row.get("feature"), row.get("status"), row.get("product_version"), row.get("build_version"), row.get("staging_environment_ref"), row.get("checks"), row.get("evidence_ref")]
        for row in clean.get("service_runs", [])
    ]
    lines.extend(_markdown_table(["服务行", "检查", "状态", "产品版本", "构建版本", "Staging", "实测断言", "证据"], service_rows))

    lines.extend(["", "## 自建环境证据", ""])
    deployment_rows = []
    for name, item in clean.get("deployment_evidence", {}).items():
        deployment_rows.append([name, item.get("status"), item.get("observed_requests"), item.get("environment_ref"), item.get("evidence_ref")])
    lines.extend(_markdown_table(["检查项", "状态", "观测值", "环境/恢复目标", "证据"], deployment_rows))
    baseline = clean.get("baseline_context", {})
    lines.extend(["", "## 历史基线（不计入当前门禁）", "", f"来源：`{baseline.get('record', '未提供')}`；提交：`{baseline.get('baseline_commit', '未提供')}`。"])
    baseline_rows = [[row.get("product"), row.get("feature"), row.get("status"), row.get("evidence_ref")] for row in baseline.get("observations", [])]
    lines.extend(_markdown_table(["产品", "功能", "历史状态", "历史证据"], baseline_rows))
    lines.append(baseline.get("note", "历史结果不自动计入当前门禁。"))

    lines.extend(["", "## 未关闭的门禁项", ""])
    if gate.blockers:
        lines.extend(f"- {redact_sensitive(blocker)}" for blocker in gate.blockers[:100])
        if len(gate.blockers) > 100:
            lines.append(f"- 另有 {len(gate.blockers) - 100} 项，详见 JSON 矩阵与逐项结果。")
    else:
        lines.append("所有 required 项均有当前版本实测证据；是否发布仍遵循仓库发布流程。")
    lines.extend([
        "",
        "## 截图与原始证据",
        "",
        f"截图/原始证据目录：`{clean.get('artifact_root', 'artifacts/acceptance/2026-09-24/')}`。",
        "Web 和后台的页面截图必须对应实际加载完成的目标页面及准确视口；操作型测试需记录隔离 staging 写入后的读回结果。",
        "安全日志仅引用已脱敏本地文件，不写入凭据、Cookie、完整媒体地址、签名 URL、卡密或稳定设备标识。",
        "",
        "逐项记录及原始字段见 [`media_matrix.json`](../../tools/qa/media_matrix.json)。",
        "",
    ])
    return "\n".join(lines)


def _fixture_value_for_metric(item: dict[str, Any]) -> Any:
    threshold = item["threshold"]
    expected = threshold["value"]
    if threshold["operator"] == "recorded_counts":
        return {"direct_count": 7, "relay_count": 3}
    if threshold["operator"] == "range_inclusive":
        return (expected[0] + expected[1]) / 2
    if threshold["operator"] == "<":
        return expected / 2
    if threshold["operator"] == ">":
        return expected + 1
    return expected


def _write_fixture_png(path: Path, width: int, height: int) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    scanline = b"\x00" + b"\x24\x48\x6c" * width
    image = _PNG_SIGNATURE
    image += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    image += chunk(b"IDAT", zlib.compress(scanline * height))
    image += chunk(b"IEND", b"")
    path.write_bytes(image)


def _fixture_screenshot_paths(temp_root: Path, row: dict[str, Any]) -> list[str]:
    dimensions = _viewport_dimensions(row.get("viewport"))
    paths = []
    for width, height in sorted(dimensions):
        path = temp_root / f"fixture-{width}x{height}.png"
        if not path.exists():
            _write_fixture_png(path, width, height)
        paths.append(str(path))
    return paths


def _fixture_readback_manifest(temp_root: Path, row_id: str) -> str:
    """Write a real structured readback manifest for the fixture-only self-test."""
    path = temp_root / f"fixture-readback-{row_id}.json"
    path.write_text(json.dumps({
        "fixture_only": True,
        "staging_environment_ref": "fixture-staging",
        "readback_items": [
            {"field": "fixture-field", "written_value": "fixture-value", "read_back_value": "fixture-value"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return path.name


def _prepare_fixture_ledger(temp_root: Path) -> dict[str, Any]:
    fixture = copy.deepcopy(load_ledger())
    fixture["self_test_mode"] = True
    fixture["staging_environment_registry"].update({
        "registered": True,
        "status": "PASS",
        "environment_ref": "fixture-staging",
        "registration_evidence_ref": "fixture://evidence/staging-registration",
    })

    for section in ("acceptance_runs", "browser_runs"):
        for row in fixture[section]:
            if not row.get("required"):
                continue
            row.update({
                "status": "PASS",
                "product_version": "fixture-version",
                "build_version": "fixture-build",
                "device_or_browser": "fixture-device",
                "network_profile": "fixture-network",
                "content_category": "fixture-content",
                "attempts": 1,
                "first_frame_ms": 1000,
                "rebuffer_count": 0,
                "auto_switch_result": "0 switches; uninterrupted source",
                "survival_minutes": 30,
                "error_log_result": "NONE (fixture only)",
                "evidence_ref": "fixture://evidence/acceptance",
                "browser_version": "fixture-browser" if row.get("product") in {"web", "admin"} else None,
            })
            if row.get("screenshot_required"):
                row["screenshot_paths"] = _fixture_screenshot_paths(temp_root, row)
                row["screenshot_reviewed"] = True
            if row.get("product") == "admin":
                row["data_scope"] = "隔离 staging；fixture-only。"
                row["staging_environment_ref"] = "fixture-staging"
            if row.get("write_readback_required"):
                row["staging_environment_ref"] = "fixture-staging"
                row["readback_status"] = "PASS"
                row["readback_evidence_ref"] = _fixture_readback_manifest(
                    temp_root, row.get("id", "fixture-row")
                )

    for row in fixture["longrun_runs"]:
        row.update({
            "status": "PASS",
            "product_version": "fixture-version",
            "build_version": "fixture-build",
            "device_or_browser": "fixture-device",
            "browser_version": "fixture-browser" if row.get("product") == "web" else None,
            "network_profile": "fixture-network",
            "content_category": "fixture-content",
            "attempts": 1,
            "first_frame_ms": 1000,
            "rebuffer_count": 0,
            "auto_switch_result": "0 switches; uninterrupted source",
            "survival_minutes": 120,
            "measurement_started_at": "2026-09-24T10:00:00Z",
            "measurement_ended_at": "2026-09-24T12:00:00Z",
            "error_log_result": "NONE (fixture only)",
            "evidence_ref": "fixture://evidence/120m",
            "screenshot_paths": _fixture_screenshot_paths(temp_root, row),
            "screenshot_reviewed": True,
            "sample_id": {
                "android_tv": "sample-movie-01",
                "android_mobile": "sample-movie-02",
                "web": "sample-movie-03",
            }.get(row.get("product")),
            "content_category": "movie",
        })

    for sample in fixture["media_samples"]:
        sample.update({
            "status": "PASS",
            "content_ref": f"fixture-content-{sample['id']}",
            "source_ref": "fixture-source",
            "source_age": "legacy" if "low_resolution_legacy_source" in sample["coverage_slot_tags"] else "current",
            "stream_type": "hls" if "hls" in sample["coverage_slot_tags"] else "direct_link" if "direct_link" in sample["coverage_slot_tags"] else "direct_link",
            "resolution": 1080 if "1080p" in sample["coverage_slot_tags"] else 720,
            "episode": "S01E01" if sample["kind"] == "series" else None,
            "authorized_test": True,
            "testability": "PASS",
            "device_or_browser": "fixture-device",
            "product_version": "fixture-version",
            "build_version": "fixture-build",
            "network_profile": "fixture-network",
            "evidence_ref": "fixture://evidence/media",
        })
        for run in sample["runs"]:
            product = run["product"]
            viewport = {
                "android_tv": "1920x1080 landscape",
                "android_mobile": "1080x2400 portrait",
                "web": "1440x900",
            }[product]
            run.update({
                "status": "PASS",
                "product_version": "fixture-version",
                "build_version": "fixture-build",
                "device_or_browser": "fixture-device",
                "browser_version": "fixture-browser" if product == "web" else None,
                "viewport": viewport,
                "network_profile": "fixture-network",
                "content_category": sample["kind"],
                "attempts": 1,
                "first_frame_ms": 1000,
                "rebuffer_count": 0,
                "auto_switch_result": "0 switches; uninterrupted source",
                "survival_minutes": run["duration_minutes"],
                "error_log_result": "NONE (fixture only)",
                "screenshot_paths": _fixture_screenshot_paths(temp_root, {**run, "viewport": viewport}),
                "screenshot_reviewed": True,
                "evidence_ref": f"fixture://evidence/{run['duration_minutes']}m",
            })

    for profile in fixture["network_profiles"]:
        profile.update({
            "status": "PASS",
            "actual_device_or_proxy": "fixture-network-shaper",
            "actual_parameters": {"fixture": True},
            "evidence_ref": "fixture://evidence/network",
            "device_or_browser": "fixture-device",
        })

    for item in fixture["performance_metrics"]:
        item.update({
            "status": "PASS",
            "actual": _fixture_value_for_metric(item),
            "sample_count": 10,
            "product_version": "fixture-version",
            "build_version": "fixture-build",
            "device_or_browser": "fixture-device",
            "evidence_ref": "fixture://evidence/performance",
        })

    deployment = fixture["deployment_evidence"]
    deployment["isolated_staging"].update({
        "status": "PASS",
        "environment_type": "isolated_staging",
        "environment_id": "acceptance-staging",
        "environment_ref": "fixture-staging",
        "write_readback_evidence": "fixture://evidence/staging-readback",
        "evidence_ref": "fixture://evidence/staging",
    })
    deployment["vps_egress"].update({
        "status": "PASS",
        "enabled_source_inventory_ref": "fixture-source-inventory",
        "sources_tested": ["fixture-source"],
        "evidence_ref": "fixture://evidence/vps-egress",
    })
    deployment["database_backup_restore"].update({
        "status": "PASS",
        "backup_ref": "fixture-backup",
        "restore_target_ref": "fixture-restore-target",
        "evidence_ref": "fixture://evidence/restore",
    })
    deployment["cloudbase_runtime_requests"].update({
        "status": "PASS",
        "observed_requests": 0,
        "observation_window": "fixture-only observation window",
        "evidence_ref": "fixture://evidence/cloudbase-zero",
    })
    deployment["object_storage_contract"].update({
        "status": "PASS",
        "evidence_ref": "fixture://evidence/object-contract",
    })
    for row in fixture["service_runs"]:
        row.update({
            "status": "PASS",
            "product_version": "fixture-version",
            "build_version": "fixture-build",
            "attempts": 1,
            "staging_environment_ref": "fixture-staging",
            "evidence_ref": "fixture://evidence/service",
        })
        if row["id"] == "importer-least-privilege":
            row["checks"] = {
                "required_insert_allowed": True,
                "privileged_read_denied": True,
                "unauthorized_write_denied": True,
                "credential_scope_reviewed": True,
            }
        else:
            row["checks"] = {
                "valid_signature_accepted": True,
                "tampered_signature_rejected": True,
                "payload_tamper_rejected": True,
                "rejected_batch_mutations": 0,
            }
    return fixture


def _run_self_test_body() -> None:
    """Exercise gate states and redaction using generated local fixtures only."""
    with tempfile.TemporaryDirectory(prefix="mytv-acceptance-fixture-") as temporary:
        temp_root = Path(temporary)
        fixture = _prepare_fixture_ledger(temp_root)
        passing = evaluate_gate(fixture, artifact_root=temp_root)
        if not passing.allowed or passing.status != "PASS":
            raise AssertionError(f"fixture PASS case failed: {passing.blockers[:5]}")

        failed_fixture = copy.deepcopy(fixture)
        failed_fixture["acceptance_runs"][0]["status"] = "FAIL"
        failed = evaluate_gate(failed_fixture, artifact_root=temp_root)
        if failed.allowed or failed.status != "FAIL":
            raise AssertionError("fixture FAIL case did not block the release gate")

        pending_fixture = copy.deepcopy(fixture)
        pending_fixture["browser_runs"][0]["status"] = "NOT-VERIFIED"
        pending = evaluate_gate(pending_fixture, artifact_root=temp_root)
        if pending.allowed or pending.status != "NOT-VERIFIED":
            raise AssertionError("fixture NOT-VERIFIED case did not block the release gate")

        private_fixture = {
            "access_token": "fixture-secret-token",
            "password": "fixture-password",
            "url": "https://media.invalid/a.m3u8?signature=fixture-signature",
            "operator": "fixture@example.invalid",
            "ordinary_status": "NOT-VERIFIED",
        }
        redacted = json.dumps(redact_sensitive(private_fixture), ensure_ascii=False)
        for private_value in ("fixture-secret-token", "fixture-password", "fixture-signature", "fixture@example.invalid"):
            if private_value in redacted:
                raise AssertionError(f"privacy filter leaked {private_value!r}")
        if "NOT-VERIFIED" not in redacted:
            raise AssertionError("privacy filtering changed the acceptance state")


def run_self_test() -> None:
    """Run the fixture-only test under a temporary, non-persistable context."""
    token = _SELF_TEST_ACTIVE.set(True)
    try:
        _run_self_test_body()
    finally:
        _SELF_TEST_ACTIVE.reset(token)


def apply_run_result(
    ledger: dict[str, Any],
    row_id: str,
    result: dict[str, Any],
    artifact_root: Path | str | None = None,
) -> dict[str, Any]:
    """Record one real/manual browser run without accepting mismatched or incomplete PASS evidence."""
    if not isinstance(result, dict):
        raise ValueError("run result must be a JSON object")
    if _contains_synthetic_data(result):
        raise ValueError("fixture or synthetic run results are accepted only by --self-test and cannot be imported")
    unknown = set(result) - RUN_RESULT_FIELDS
    if unknown:
        raise ValueError(f"unsupported result fields: {', '.join(sorted(unknown))}")
    matches = [row for row in ledger.get("browser_runs", []) if row.get("id") == row_id]
    if len(matches) != 1:
        raise ValueError(f"browser run ID must identify exactly one row: {row_id}")
    row = matches[0]
    if not row.get("required"):
        raise ValueError("only required browser rows can be recorded through this command")

    status = result.get("status")
    if status not in VALID_STATUSES:
        raise ValueError("result status must be PASS, FAIL, or NOT-VERIFIED")
    if status == "N/A":
        raise ValueError("required browser workflow cannot be recorded as N/A")
    supplied_viewport = result.get("viewport")
    if status == "PASS" and not supplied_viewport:
        raise ValueError("PASS result must report the actual viewport")
    if supplied_viewport and supplied_viewport != row.get("viewport"):
        raise ValueError(f"viewport mismatch for {row_id}: expected {row.get('viewport')}, got {supplied_viewport}")

    safe_result = redact_sensitive(result)
    root = Path(artifact_root) if artifact_root is not None else ROOT
    evidence_path = _local_artifact_path(safe_result.get("evidence_ref"), root)
    if evidence_path is not None and evidence_path.is_file() and evidence_path.suffix.lower() == ".json":
        try:
            evidence_document = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            evidence_document = None
        if evidence_document is not None and _contains_synthetic_data(evidence_document):
            raise ValueError("fixture or synthetic evidence cannot be imported through a normal result")
    screenshots = safe_result.get("screenshot_paths")
    if screenshots is not None:
        if not isinstance(screenshots, list):
            raise ValueError("screenshot_paths must be a list of local artifact paths")
        normalized_paths = []
        root_resolved = root.resolve()
        for screenshot in screenshots:
            if not isinstance(screenshot, str) or not screenshot.strip():
                raise ValueError("screenshot_paths must contain non-empty local paths")
            resolved_path = _clean_path(screenshot, root)
            try:
                normalized_paths.append(resolved_path.relative_to(root_resolved).as_posix())
            except ValueError as exc:
                raise ValueError("screenshots must be stored under the artifact root") from exc
        safe_result["screenshot_paths"] = normalized_paths
    contains_redaction = "[REDACTED" in json.dumps(safe_result, ensure_ascii=False)
    if status == "PASS" and contains_redaction:
        raise ValueError("PASS result contains a value redacted for privacy; replace it with a safe reference")

    candidate = dict(row)
    candidate.update(safe_result)
    candidate["result_history"] = list(row.get("result_history") or [])
    candidate["result_history"].append({
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **safe_result,
    })
    if status == "PASS":
        local_blockers: list[str] = []
        _check_run_row(
            candidate,
            f"{candidate.get('product')} {candidate.get('feature')}",
            local_blockers,
            root,
            browser_run=True,
            staging_identity=ledger.get("staging_environment_registry"),
        )
        if local_blockers:
            raise ValueError("PASS result is incomplete: " + "; ".join(local_blockers))
    row.clear()
    row.update(candidate)
    return row


def write_ledger(path: Path | str, ledger: dict[str, Any]) -> None:
    """Atomically save the ledger after a recursive privacy pass."""
    if _contains_synthetic_data(ledger):
        raise ValueError("fixture or synthetic acceptance data cannot be persisted")
    ledger_path = Path(path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = redact_sensitive(ledger)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=ledger_path.parent,
            prefix=f".{ledger_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temp_name = temporary.name
            json.dump(cleaned, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        os.replace(temp_name, ledger_path)
        temp_name = None
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def render_browser_steps(ledger: dict[str, Any], row_id: str) -> str:
    rows = [row for row in ledger.get("browser_runs", []) if row.get("id") == row_id]
    if len(rows) != 1:
        raise ValueError(f"browser run ID must identify exactly one row: {row_id}")
    row = redact_sensitive(rows[0])
    lines = [
        f"# Manual browser E2E steps: {row.get('product')} / {row.get('feature')}",
        "",
        f"- Row ID: `{row_id}`",
        f"- Status: `{row.get('status')}`",
        f"- Target viewport: `{row.get('viewport')}`",
        f"- Execution method: `{row.get('execution_method')}`",
        "- These are instructions only; this command does not execute or pass the test.",
        "",
        "## Steps",
    ]
    lines.extend(f"{index}. {step}" for index, step in enumerate(row.get("manual_steps", []), 1))
    lines.extend(["", "## Expected results"])
    lines.extend(f"- {result}" for result in row.get("expected_results", []))
    lines.extend([
        "",
        "After the real browser run, capture a screenshot of the loaded target page and submit a structured result with `--record-result`.",
        "The evidence manifest must exist under the artifact root and bind `row_id`, `product_version`, `build_version`, `viewport`, screenshot paths, SHA-256 hashes, and pixel dimensions.",
        "For staging write operations, the registered isolated-staging identity and readback evidence must match the manifest; production references are rejected.",
        "",
    ])
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help="acceptance JSON path")
    parser.add_argument("--artifact-root", type=Path, default=ROOT, help="root directory for screenshots and local evidence")
    parser.add_argument("--report", type=Path, help="write the privacy-filtered Markdown report")
    parser.add_argument("--self-test", action="store_true", help="run fixture-only gate and privacy checks")
    parser.add_argument("--show-steps", metavar="ROW_ID", help="print one Web/admin manual browser E2E checklist")
    parser.add_argument("--record-result", type=Path, help="import one structured real browser result JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.self_test:
        try:
            run_self_test()
        except Exception as exc:  # CLI reports a concise self-test failure.
            print(f"Acceptance runner self-test FAIL (fixture-only): {exc}", file=sys.stderr)
            return 1
        print("Acceptance runner self-test PASS (fixture-only; PASS/FAIL/NOT-VERIFIED and privacy filtering checked; no real product acceptance).")
        return 0

    if args.show_steps:
        try:
            print(render_browser_steps(load_ledger(args.ledger), args.show_steps))
        except ValueError as exc:
            print(f"Browser checklist error: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.record_result:
        try:
            ledger = load_ledger(args.ledger)
            result_document = json.loads(args.record_result.read_text(encoding="utf-8"))
            if not isinstance(result_document, dict):
                raise ValueError("result JSON root must be an object")
            row_id = result_document.pop("row_id", None)
            if not isinstance(row_id, str) or not row_id.strip():
                raise ValueError("result JSON must include row_id")
            recorded = apply_run_result(ledger, row_id, result_document, artifact_root=args.artifact_root)
            write_ledger(args.ledger, ledger)
            gate = evaluate_gate(ledger, artifact_root=args.artifact_root)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"Browser result not recorded: {exc}", file=sys.stderr)
            return 2
        print(f"Recorded {recorded['id']} as {recorded['status']}; overall acceptance gate remains {gate.status} ({len(gate.blockers)} blockers).")
        return 0

    try:
        ledger = load_ledger(args.ledger)
    except ValueError as exc:
        print(f"Acceptance ledger error: {exc}", file=sys.stderr)
        return 2

    gate = evaluate_gate(ledger, artifact_root=args.artifact_root)
    report = render_report(ledger, gate, artifact_root=args.artifact_root)
    if args.report:
        report_path = args.report if args.report.is_absolute() else ROOT / args.report
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        print(f"Acceptance gate {gate.status}: {'allowed' if gate.allowed else f'blocked by {len(gate.blockers)} item(s)'}; report written to {report_path}")
    else:
        print(f"Acceptance gate {gate.status}: {'allowed' if gate.allowed else f'blocked by {len(gate.blockers)} item(s)'}")
        for blocker in gate.blockers[:12]:
            print(f"- {redact_sensitive(blocker)}")
        if len(gate.blockers) > 12:
            print(f"- ... {len(gate.blockers) - 12} additional blocker(s); use --report to write the full record")
    return 0 if gate.allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())

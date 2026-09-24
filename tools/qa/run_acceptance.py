#!/usr/bin/env python3
"""Validate the My TV acceptance ledger and generate a privacy-filtered report."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
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
    return ledger


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
        if not str(row.get("staging_environment_ref") or "").strip():
            blockers.append(f"{label}: isolated staging environment reference is missing")
    if row.get("write_readback_required"):
        if row.get("readback_status") != "PASS":
            blockers.append(f"{label}: staging write readback status is {row.get('readback_status') or 'missing'}")
        if not str(row.get("staging_environment_ref") or "").strip():
            blockers.append(f"{label}: isolated staging environment reference is missing")
        if not str(row.get("readback_evidence_ref") or "").strip():
            blockers.append(f"{label}: staging readback evidence is missing")

    if not _is_safe_reference(row.get("evidence_ref")):
        blockers.append(f"{label}: missing evidence_ref")
    if not str(row.get("error_log_result") or "").strip() and not _is_safe_reference(row.get("error_log_ref")):
        blockers.append(f"{label}: error log result/reference was not recorded")

    if row.get("screenshot_required"):
        screenshot_paths = row.get("screenshot_paths")
        if not isinstance(screenshot_paths, list) or not screenshot_paths:
            blockers.append(f"{label}: required screenshot is missing")
        else:
            for screenshot in screenshot_paths:
                if not isinstance(screenshot, str) or not screenshot.strip():
                    blockers.append(f"{label}: screenshot path is empty")
                    continue
                screenshot_path = _clean_path(screenshot, artifact_root)
                if not screenshot_path.is_file() or screenshot_path.stat().st_size <= 0:
                    blockers.append(f"{label}: screenshot file does not exist or is empty")
                    break
            if row.get("screenshot_reviewed") is not True:
                blockers.append(f"{label}: screenshot content/viewport review is missing")


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
            if not _is_number(run.get("first_frame_ms")) or not _is_number(run.get("rebuffer_count")):
                return False
            if not str(run.get("auto_switch_result") or "").strip():
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


def _check_deployment(ledger: dict[str, Any], blockers: list[str]) -> None:
    evidence = ledger.get("deployment_evidence")
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
            if evidence_id == "isolated_staging":
                if item.get("environment_type") != "isolated_staging":
                    blockers.append(f"{label}: environment is not confirmed as isolated staging")
                if not item.get("environment_ref") or not item.get("write_readback_evidence"):
                    blockers.append(f"{label}: staging write/readback evidence is missing")
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
            _check_run_row(row, label, blockers, root, browser_run=collection == "browser_runs")
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
            _check_run_row(row, label, blockers, root)
    _check_browser_coverage(ledger, blockers)
    _check_media(ledger, blockers, root)
    _check_network(ledger, blockers)
    _check_metrics(ledger, blockers)
    _check_deployment(ledger, blockers)

    # A missing or malformed section is itself a blocker, not an implicit pass.
    if not isinstance(ledger.get("media_coverage_targets"), dict):
        blockers.append("media: coverage target declarations are missing")
    scope = ledger.get("scope")
    if not isinstance(scope, dict) or scope.get("legacy_apk") != "旧 APK 不在本次支持范围。":
        blockers.append("scope: legacy APK support boundary is missing or changed")

    blockers = list(dict.fromkeys(blockers))
    if not blockers:
        return GateResult(allowed=True, blockers=[], status="PASS")
    has_failure = any(
        isinstance(row, dict) and row.get("status") == "FAIL"
        for section in ("acceptance_runs", "browser_runs", "longrun_runs", "media_samples", "network_profiles", "performance_metrics")
        for row in ledger.get(section, [])
    )
    cloudbase = ledger.get("deployment_evidence", {}).get("cloudbase_runtime_requests", {})
    if isinstance(cloudbase, dict) and isinstance(cloudbase.get("observed_requests"), (int, float)) and cloudbase["observed_requests"] > 0:
        has_failure = True
    return GateResult(allowed=False, blockers=blockers, status="FAIL" if has_failure else "NOT-VERIFIED")


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


def render_report(ledger: dict[str, Any], gate: GateResult | None = None) -> str:
    """Render a traceable report after redacting the complete input tree."""
    clean = redact_sensitive(ledger)
    gate = gate or evaluate_gate(clean)
    total_required = sum(
        1
        for section in ("acceptance_runs", "browser_runs", "longrun_runs", "media_samples", "network_profiles", "performance_metrics")
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
        "先按每条 JSON 行中的 `manual_steps` 和 `expected_results` 在 Chrome staging 实测，再保存结构化版本/视口/网络/首帧/缓冲/截图/错误日志结果。PASS 必须有已加载目标页面的非空截图、人工内容检查、当前产品/构建/浏览器版本及证据引用；视口必须匹配 row ID。后台写操作另外需要隔离 staging 环境引用和逐项读回状态/证据。结果导入不会解除其他未验证行的门禁。",
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


def _prepare_fixture_ledger(temp_root: Path) -> dict[str, Any]:
    fixture = copy.deepcopy(load_ledger())
    screenshot = temp_root / "fixture-screen.png"
    screenshot.write_bytes(b"fixture-only-not-a-product-screenshot")

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
                "auto_switch_result": "fixture-only observation",
                "survival_minutes": 30,
                "error_log_result": "NONE (fixture only)",
                "evidence_ref": "fixture://evidence/acceptance",
                "browser_version": "fixture-browser" if row.get("product") in {"web", "admin"} else None,
            })
            if row.get("screenshot_required"):
                row["screenshot_paths"] = [str(screenshot)]
                row["screenshot_reviewed"] = True
            if row.get("product") == "admin":
                row["data_scope"] = "隔离 staging；fixture-only。"
                row["staging_environment_ref"] = "fixture-staging"
            if row.get("write_readback_required"):
                row["staging_environment_ref"] = "fixture-staging"
                row["readback_status"] = "PASS"
                row["readback_evidence_ref"] = "fixture://evidence/staging-readback"

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
            "survival_minutes": 120,
            "error_log_result": "NONE (fixture only)",
            "evidence_ref": "fixture://evidence/120m",
            "screenshot_paths": [str(screenshot)],
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
            run.update({
                "status": "PASS",
                "product_version": "fixture-version",
                "build_version": "fixture-build",
                "device_or_browser": "fixture-device",
                "browser_version": "fixture-browser" if product == "web" else None,
                "viewport": {
                    "android_tv": "1920x1080 landscape",
                    "android_mobile": "1080x2400 portrait",
                    "web": "1440x900",
                }[product],
                "network_profile": "fixture-network",
                "content_category": sample["kind"],
                "attempts": 1,
                "first_frame_ms": 1000,
                "rebuffer_count": 0,
                "auto_switch_result": "not triggered; fixture only",
                "survival_minutes": run["duration_minutes"],
                "error_log_result": "NONE (fixture only)",
                "screenshot_paths": [str(screenshot)],
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
    return fixture


def run_self_test() -> None:
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


def apply_run_result(
    ledger: dict[str, Any],
    row_id: str,
    result: dict[str, Any],
    artifact_root: Path | str | None = None,
) -> dict[str, Any]:
    """Record one real/manual browser run without accepting mismatched or incomplete PASS evidence."""
    if not isinstance(result, dict):
        raise ValueError("run result must be a JSON object")
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
        _check_run_row(candidate, f"{candidate.get('product')} {candidate.get('feature')}", local_blockers, root, browser_run=True)
        if local_blockers:
            raise ValueError("PASS result is incomplete: " + "; ".join(local_blockers))
    row.clear()
    row.update(candidate)
    return row


def write_ledger(path: Path | str, ledger: dict[str, Any]) -> None:
    """Atomically save the ledger after a recursive privacy pass."""
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
        "For staging write operations, include the isolated environment reference plus `readback_status=PASS` and readback evidence.",
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
    report = render_report(ledger, gate)
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

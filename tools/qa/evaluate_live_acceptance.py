#!/usr/bin/env python3
"""Evaluate football-live release acceptance evidence.

The evaluator deliberately treats missing, weak, or ambiguous evidence as a
failure. It never upgrades a short run, a single match, process liveness, or a
low-confidence image match into a release pass.

The companion PowerShell runner captures:
  matrix.json
  orchestration.json
  runs/<target>/run.json, result.json, samples.jsonl, logcat.txt, ...
  delay-probes.json
  delay/<phase>/<matchId>.json

Existing compare_live_delay.py reports are consumed as evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import statistics
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


VARIANTS = {"tv", "mobile"}
PLAYBACK_ERROR_PATTERNS = [
    r"Playback error",
    r"PlaybackException",
    r"BehindLiveWindowException",
    r"ERROR_CODE_BEHIND_LIVE_WINDOW",
    r"engine playerError:",
    r"vlc error:",
    r"vlc prepare failed",
    r"bootLive 无可用信号",
    r"(?:做直播|足球直播|球迹直播)初始化失败",
    r"直播线路刷新失败",
    r"直播 READY 后 .* 无首帧",
    r"缓冲 .* 无增长",
    r"画面冻结",
    r"live HLS window ended",
    r"vlc end reached",
    r"engine 落后直播窗口",
]
FATAL_PATTERNS = [
    r"FATAL EXCEPTION",
    r"FATAL ANDROID",
    r"AndroidRuntime.*(?:FATAL|Fatal exception)",
]
FIRST_FRAME_PATTERNS = [
    r"engine 首帧已渲染",
    r"vlc vout established",
    r"\[first_frame\]",
    r"READY .*（出画面）",
]
BUFFERING_HEARTBEAT_RE = re.compile(r"hb (?:vlc )?BUFFERING\b")
HEARTBEAT_DETAIL_RE = re.compile(
    r"hb (?:vlc )?(READY|BUFFERING).*playing=(true|false).*"
    r"rendered=(-?\d+)"
)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def read_samples(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def normalized_pid(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return " ".join(sorted(str(value).split()))


def count_patterns(text: str, patterns: list[str]) -> int:
    return sum(len(re.findall(pattern, text, flags=re.IGNORECASE | re.MULTILINE))
               for pattern in patterns)


def safe_median(values: list[float]) -> float | None:
    return round(float(statistics.median(values)), 3) if values else None


def gate(ok: bool, **details: Any) -> dict[str, Any]:
    return {"pass": bool(ok), **details}


def evidence_dir(root: Path, target: dict[str, Any]) -> Path:
    raw = target.get("evidenceDirectory") or f"runs/{target.get('id', 'target')}"
    path = Path(str(raw))
    return path if path.is_absolute() else root / path


def target_identity_ok(target: dict[str, Any]) -> bool:
    return (
        isinstance(target.get("serial"), str)
        and bool(target["serial"].strip())
        and target.get("package") in {
            "com.qiubo.optimaltv.public",
            "com.qiubo.optimaltv.mobile.public",
        }
        and isinstance(target.get("matchId"), str)
        and re.fullmatch(r"\d+", target["matchId"]) is not None
        and target.get("variant") in VARIANTS
        and str(target.get("id", "")).strip() != ""
    )


def evaluate_target(
    root: Path,
    target: dict[str, Any],
    candidates: dict[str, Any],
    required_seconds: int,
    require_full_duration: bool,
) -> tuple[dict[str, Any], list[str]]:
    directory = evidence_dir(root, target)
    run = load_json(directory / "run.json", {}) or {}
    result = load_json(directory / "result.json", {}) or {}
    samples = read_samples(directory / "samples.jsonl")
    log_text = read_text(directory / "logcat.txt")
    crash_text = read_text(directory / "crash-buffer.txt")
    events_text = read_text(directory / "events.txt")
    post_pid = read_text(directory / "post-pid.txt").strip()
    first_frame_png = directory / "first-frame.png"
    package = str(target.get("package", ""))
    failures: list[str] = []
    gates: dict[str, Any] = {}

    expected_identity = {
        "serial": str(target.get("serial", "")),
        "package": package,
        "matchId": str(target.get("matchId", "")),
    }
    identity_ok = bool(run) and bool(samples) and (bool(result) or not require_full_duration)
    if run:
        identity_ok = identity_ok and all(str(run.get(k, "")) == v for k, v in expected_identity.items())
    if result:
        identity_ok = identity_ok and all(str(result.get(k, "")) == v for k, v in expected_identity.items())
    if samples:
        identity_ok = identity_ok and all(
            all(str(row.get(k, "")) == v for k, v in expected_identity.items())
            for row in samples
        )
    gates["evidenceAndIdentity"] = gate(
        identity_ok,
        evidenceDirectory=str(directory),
        hasRun=bool(run),
        hasResult=bool(result),
        sampleCount=len(samples),
    )
    if not identity_ok:
        failures.append(f"{target.get('id')}: evidence or target identity is incomplete")

    first_observed = next((row for row in samples if row.get("firstFrameObserved") is True), None)
    first_frame_ok = (
        first_frame_png.is_file()
        and first_frame_png.stat().st_size > 0
        and first_observed is not None
        and count_patterns(log_text, FIRST_FRAME_PATTERNS) > 0
    )
    gates["firstFrame"] = gate(
        first_frame_ok,
        screenshotBytes=first_frame_png.stat().st_size if first_frame_png.is_file() else 0,
        observedSampleTime=first_observed.get("time") if first_observed else None,
        firstFrameLogMatches=count_patterns(log_text, FIRST_FRAME_PATTERNS),
    )
    if not first_frame_ok:
        failures.append(f"{target.get('id')}: first frame is not proven")

    result_pids = [normalized_pid(result.get("startPid")), normalized_pid(result.get("finalPid"))]
    sampled_original_pids = {
        normalized_pid(row.get("originalPid")) for row in samples
        if normalized_pid(row.get("originalPid"))
    }
    sampled_current_pids = {
        normalized_pid(row.get("pid")) for row in samples
        if normalized_pid(row.get("pid"))
    }
    full_pids = [pid for pid in [*result_pids, normalized_pid(post_pid)] if pid]
    if require_full_duration:
        process_ok = (
            bool(full_pids)
            and len(set(full_pids)) == 1
            and len(sampled_original_pids) == 1
            and sampled_original_pids == sampled_current_pids
            and not any(row.get("processChanged") is True for row in samples)
        )
    else:
        process_ok = (
            bool(sampled_original_pids)
            and len(sampled_original_pids) == 1
            and sampled_original_pids == sampled_current_pids
            and not any(row.get("processChanged") is True for row in samples)
        )
    gates["processUnchanged"] = gate(
        process_ok,
        startPid=result_pids[0],
        finalPid=result_pids[1],
        postPid=normalized_pid(post_pid),
        sampledOriginalPids=sorted(sampled_original_pids),
        sampledCurrentPids=sorted(sampled_current_pids),
        sampledPidChanges=sum(1 for row in samples if row.get("processChanged") is True),
    )
    if not process_ok:
        failures.append(f"{target.get('id')}: app process changed or disappeared")

    app_log_fatal = count_patterns(log_text, FATAL_PATTERNS)
    # The crash buffer is system-wide. Only count it when the app package is
    # present in the crash evidence, so an unrelated system app cannot fail QA.
    crash_buffer_fatal = (
        count_patterns(crash_text, FATAL_PATTERNS) if package in crash_text else 0
    )
    fatal_matches = app_log_fatal + crash_buffer_fatal
    anr_matches = (
        len(re.findall(rf"ANR in\s+{re.escape(package)}\b", crash_text, flags=re.IGNORECASE))
        + len(re.findall(rf"am_anr.*{re.escape(package)}\b", events_text, flags=re.IGNORECASE))
    )
    crash_ok = fatal_matches == 0 and anr_matches == 0
    gates["noFatalOrAnr"] = gate(
        crash_ok,
        fatalMatches=fatal_matches,
        anrMatches=anr_matches,
        crashBufferBytes=len(crash_text.encode("utf-8")),
        eventsBytes=len(events_text.encode("utf-8")),
    )
    if not crash_ok:
        failures.append(f"{target.get('id')}: FATAL/crash/ANR evidence found")

    playback_error_matches = count_patterns(log_text, PLAYBACK_ERROR_PATTERNS)
    result_playback_errors = int(result.get("playbackErrors") or 0)
    sample_playback_errors = max(
        [int(row.get("playbackErrors") or 0) for row in samples] or [0]
    )
    source_failures = max([int(row.get("sourceFailures") or 0) for row in samples] or [0])
    playback_ok = playback_error_matches == 0 and result_playback_errors == 0 and sample_playback_errors == 0
    gates["noPlaybackErrors"] = gate(
        playback_ok,
        logMatches=playback_error_matches,
        resultCount=result_playback_errors,
        sampledCount=sample_playback_errors,
        sourceFailureCount=source_failures,
    )
    if not playback_ok or source_failures:
        failures.append(f"{target.get('id')}: playback/source errors found")

    log_rebuffers = len(re.findall(r"engine rebuffer #", log_text, flags=re.IGNORECASE))
    result_rebuffers = int(result.get("rebuffers") or 0)
    sample_rebuffers = max([int(row.get("rebuffers") or 0) for row in samples] or [0])
    rebuffer_ok = log_rebuffers == 0 and result_rebuffers == 0 and sample_rebuffers == 0
    gates["rebufferZero"] = gate(
        rebuffer_ok,
        logEvents=log_rebuffers,
        resultCount=result_rebuffers,
        maxSampledCount=sample_rebuffers,
    )
    if not rebuffer_ok:
        failures.append(f"{target.get('id')}: rebuffer count is not zero")

    playback_rows = [row for row in samples if row.get("firstFrameObserved") is True]
    result_observed_seconds = int(result.get("observedPlaybackSeconds") or 0)
    sample_observed_seconds = max(
        [int(row.get("observedPlaybackSeconds") or 0) for row in playback_rows] + [0]
    )
    # The monitor's final result and its streamed samples must agree. Taking the
    # minimum prevents a stale/forged sample from extending a short run.
    observed_seconds = (
        min(result_observed_seconds, sample_observed_seconds)
        if result_observed_seconds and sample_observed_seconds
        else max(result_observed_seconds, sample_observed_seconds)
    )
    requested_minutes = int(result.get("requestedMinutes") or run.get("requestedMinutes") or 0)
    duration_ok = observed_seconds >= required_seconds and requested_minutes * 60 >= required_seconds
    if require_full_duration:
        gates["continuousPlayback"] = gate(
            duration_ok,
            observedPlaybackSeconds=observed_seconds,
            requiredSeconds=required_seconds,
            requestedMinutes=requested_minutes,
        )
        if not duration_ok:
            failures.append(f"{target.get('id')}: continuous duration is below {required_seconds}s")
    else:
        gates["continuousPlayback"] = gate(True, skipped="early-phase", observedPlaybackSeconds=observed_seconds)

    heartbeat_rows: list[dict[str, Any]] = []
    for row in playback_rows:
        heartbeat = row.get("heartbeat") or {}
        rendered = heartbeat.get("renderedFrames")
        state = heartbeat.get("state")
        playing = heartbeat.get("playing")
        if rendered is None or state is None:
            detail = str(row.get("lastHeartbeat") or "")
            match = HEARTBEAT_DETAIL_RE.search(detail)
            if match:
                state = match.group(1)
                playing = match.group(2) == "true"
                rendered = int(match.group(3))
        at = parse_time(row.get("time"))
        if at is not None and rendered is not None:
            heartbeat_rows.append({
                "time": at,
                "rendered": int(rendered),
                "state": state,
                "playing": playing,
            })

    valid_render_rows = [row for row in heartbeat_rows if row["rendered"] >= 0]
    expected_samples = max(1, math.floor(observed_seconds / 30) - 2)
    coverage_ok = len(valid_render_rows) >= max(2, min(expected_samples, math.ceil(expected_samples * 0.8)))
    monotonic_ok = (
        bool(valid_render_rows)
        and valid_render_rows[0]["rendered"] >= 0
        and valid_render_rows[-1]["rendered"] > valid_render_rows[0]["rendered"]
        and all(
            later["rendered"] >= earlier["rendered"]
            for earlier, later in zip(valid_render_rows, valid_render_rows[1:])
        )
    )
    max_stagnation = 0.0
    previous: dict[str, Any] | None = None
    for row in valid_render_rows:
        if previous is not None:
            if row["rendered"] == previous["rendered"]:
                max_stagnation = max(max_stagnation, (row["time"] - previous["time"]).total_seconds())
            else:
                previous = row
        else:
            previous = row
    gap_values = [
        (later["time"] - earlier["time"]).total_seconds()
        for earlier, later in zip(heartbeat_rows, heartbeat_rows[1:])
    ]
    max_sample_gap = max(gap_values or [0.0])
    ready_playing = [
        row for row in valid_render_rows
        if row.get("state") == "READY" and row.get("playing") is True
    ]
    ready_ratio = len(ready_playing) / len(valid_render_rows) if valid_render_rows else 0.0
    buffering_samples = sum(
        1 for row in playback_rows if BUFFERING_HEARTBEAT_RE.search(str(row.get("lastHeartbeat") or ""))
    )
    render_ok = (
        coverage_ok
        and monotonic_ok
        and max_stagnation <= 75.0
        and max_sample_gap <= 75.0
        and ready_ratio >= 0.9
        and buffering_samples == 0
    )
    gates["renderedFrameProgress"] = gate(
        render_ok,
        validSamples=len(valid_render_rows),
        expectedSamples=expected_samples,
        firstRendered=valid_render_rows[0]["rendered"] if valid_render_rows else None,
        lastRendered=valid_render_rows[-1]["rendered"] if valid_render_rows else None,
        monotonic=monotonic_ok,
        maxRenderedStagnationSeconds=round(max_stagnation, 3),
        maxSampleGapSeconds=round(max_sample_gap, 3),
        readyPlayingRatio=round(ready_ratio, 3),
        bufferingSamples=buffering_samples,
    )
    if not render_ok:
        failures.append(f"{target.get('id')}: frame progress/buffering evidence does not prove uninterrupted video")

    start_session_lines = [
        int(value) for value in re.findall(r"startSession line=(\d+)", log_text)
    ]
    auto_switches = len(re.findall(r"\[auto_switch\]|自动切|→ 切备用线", log_text))
    source_line_ok = bool(start_session_lines) and all(value == 0 for value in start_session_lines) and auto_switches == 0
    gates["referenceSourceLine"] = gate(
        source_line_ok,
        startSessionLines=start_session_lines,
        requiresDefaultSourceIndex=0,
        autoSwitchEvents=auto_switches,
    )
    if not source_line_ok:
        failures.append(f"{target.get('id')}: playback source is not pinned to the reference source index 0")

    variant = str(target.get("variant", ""))
    candidate = candidates.get(variant) if isinstance(candidates, dict) else None
    candidate = candidate if isinstance(candidate, dict) else {}
    expected_apk = str(candidate.get("apkSha256", "")).lower()
    actual_apk = str(run.get("apkSha256", "")).lower()
    apk_ok = bool(re.fullmatch(r"[0-9a-f]{64}", expected_apk)) and actual_apk == expected_apk
    gates["apkIdentity"] = gate(apk_ok, expectedSha256=expected_apk, installedSha256=actual_apk)
    if not apk_ok:
        failures.append(f"{target.get('id')}: installed APK does not match the candidate")

    return {
        "id": target.get("id"),
        "serial": target.get("serial"),
        "package": package,
        "variant": variant,
        "matchId": target.get("matchId"),
        "label": target.get("label"),
        "pass": all(item.get("pass") is True for item in gates.values()),
        "observedPlaybackSeconds": observed_seconds,
        "gates": gates,
    }, failures


def candidate_gates(matrix: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    candidates = matrix.get("candidates") if isinstance(matrix.get("candidates"), dict) else {}
    gates: dict[str, Any] = {}
    failures: list[str] = []
    for variant in sorted(VARIANTS):
        candidate = candidates.get(variant) if isinstance(candidates, dict) else None
        candidate = candidate if isinstance(candidate, dict) else {}
        apk_sha = str(candidate.get("apkSha256", "")).lower()
        embedded = str(candidate.get("embeddedBackendProxySha256", "")).lower()
        source = str(candidate.get("sourceProxySha256", "")).lower()
        ok = (
            re.fullmatch(r"[0-9a-f]{64}", apk_sha) is not None
            and re.fullmatch(r"[0-9a-f]{64}", embedded) is not None
            and embedded == source
            and candidate.get("backendMatchesSource") is True
        )
        gates[variant] = gate(
            ok,
            apkFileName=candidate.get("apkFileName"),
            apkSha256=apk_sha,
            embeddedBackendProxySha256=embedded,
            sourceProxySha256=source,
            backendMatchesSource=candidate.get("backendMatchesSource") is True,
        )
        if not ok:
            failures.append(f"{variant}: candidate APK/backend identity is missing or stale")
    return gates, failures, candidates



def evaluate_delay(
    root: Path,
    matrix: dict[str, Any],
    orchestration: dict[str, Any],
    phase: str,
    max_delay: float,
    minimum_samples: int,
    max_growth: float,
    required_seconds: int,
) -> tuple[dict[str, Any], list[str]]:
    manifest = load_json(root / "delay-probes.json", {}) or {}
    probes = manifest.get("probes") if isinstance(manifest, dict) else manifest
    probes = [item for item in (probes or []) if isinstance(item, dict)]
    allowed_phases = {"initial", "final"} if phase in {"final", "all"} else {"initial"}
    probes = [item for item in probes if item.get("phase") in allowed_phases]
    targets = [item for item in matrix.get("targets", []) if isinstance(item, dict)]
    match_ids = sorted({str(item.get("matchId")) for item in targets})
    failures: list[str] = []
    details: dict[str, Any] = {}

    ready_at = parse_time(orchestration.get("allTargetsReadyAt"))
    completed_at = parse_time(orchestration.get("completedAt"))
    by_phase_match: dict[tuple[str, str], dict[str, Any]] = {}
    for probe in probes:
        key = (str(probe.get("phase")), str(probe.get("matchId")))
        if key in by_phase_match:
            failures.append(f"duplicate delay probe {key[0]}/{key[1]}")
        by_phase_match[key] = probe

    for match_id in match_ids:
        for needed_phase in sorted(allowed_phases):
            if (needed_phase, match_id) not in by_phase_match:
                failures.append(f"missing {needed_phase} delay probe for match {match_id}")

    initial_timing_ok = True
    final_timing_ok = True
    if ready_at is None:
        initial_timing_ok = False
        final_timing_ok = False
        failures.append("allTargetsReadyAt is missing")
    else:
        for probe in probes:
            started = parse_time(probe.get("startedAt"))
            finished = parse_time(probe.get("finishedAt"))
            if started is None or finished is None or finished < started:
                failures.append(f"delay probe timestamps invalid: {probe.get('phase')}/{probe.get('matchId')}")
                continue
            if probe.get("phase") == "initial":
                initial_timing_ok = initial_timing_ok and started >= ready_at and finished <= ready_at + timedelta(minutes=5)
            elif probe.get("phase") == "final":
                final_timing_ok = final_timing_ok and started >= ready_at + timedelta(seconds=required_seconds)
                if completed_at is not None:
                    final_timing_ok = final_timing_ok and finished <= completed_at

    target_reports: dict[str, Any] = {}
    for target in targets:
        target_id = str(target.get("id"))
        serial = str(target.get("serial"))
        match_id = str(target.get("matchId"))
        serial_results: dict[str, Any] = {}
        source_ids: dict[str, str] = {}
        for needed_phase in sorted(allowed_phases):
            probe = by_phase_match.get((needed_phase, match_id))
            if not probe:
                serial_results[needed_phase] = gate(False, error="probe missing")
                continue
            report_path = Path(str(probe.get("report", "")))
            if not report_path.is_absolute():
                report_path = root / report_path
            report = load_json(report_path, {}) or {}
            source_id = str(report.get("sourceId") or "").strip()
            source_ids[needed_phase] = source_id
            samples = [row for row in report.get("samples", []) if isinstance(row, dict)]
            target_samples = [row for row in samples if str(row.get("serial")) == serial]
            # A stale observation is evidence against the window even if its
            # image match is weak and would otherwise be filtered out.
            stale_reference_seen = any(
                isinstance(row.get("referenceHeadIdleSeconds"), (int, float))
                and row["referenceHeadIdleSeconds"] > 8.0
                for row in target_samples
            )
            try:
                reference_fps = float(report.get("referenceFps"))
                probe_started = parse_time(probe.get("startedAt"))
                probe_finished = parse_time(probe.get("finishedAt"))
                probe_seconds = (probe_finished - probe_started).total_seconds()
                if not math.isfinite(reference_fps) or reference_fps <= 0 or probe_seconds <= 0:
                    raise ValueError("invalid reference timing")
            except (TypeError, ValueError, AttributeError):
                reference_fps = 0.0
                probe_seconds = 0.0
            strong = []
            for row in target_samples:
                try:
                    delay = float(row.get("addedDelaySeconds"))
                    image_difference = float(row.get("imageDifference"))
                    margin = float(row.get("alternativeMargin"))
                    timing_span = float(row.get("screenshotTimingSpanSeconds"))
                    elapsed = float(row.get("elapsedSeconds"))
                    idle = float(row.get("referenceHeadIdleSeconds"))
                    head = int(row["referenceHeadFrameIndex"])
                    end_head = int(row["referenceHeadFrameIndexAtScreenshotEnd"])
                    matched = int(row["matchedReferenceFrameIndex"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if (
                    row.get("distinctMatch") is True
                    and all(math.isfinite(value) for value in (delay, image_difference, margin, timing_span, elapsed, idle))
                    and 0 <= delay
                    and 0 <= image_difference <= 2.0
                    and margin >= 3.0
                    and 0 <= timing_span <= 2.5
                    and 0 <= elapsed <= probe_seconds
                    and 0 <= idle <= 8.0
                    and reference_fps > 0
                    and 0 <= matched <= head
                    and end_head >= head
                    and abs(delay - (head - matched) / reference_fps) <= 0.001
                ):
                    strong.append(row)
            delays = [float(row["addedDelaySeconds"]) for row in strong]
            inconsistent_distinct_samples = sum(
                1 for row in target_samples if row.get("distinctMatch") is True and row not in strong
            )
            upper_delays = [(int(row["referenceHeadFrameIndexAtScreenshotEnd"]) -
                             int(row["matchedReferenceFrameIndex"]) + 1) / reference_fps
                            for row in strong]
            reference_head_indices = [int(row["referenceHeadFrameIndex"]) for row in strong]
            sample_times = [float(row["elapsedSeconds"]) for row in strong]
            sample_span = sample_times[-1] - sample_times[0] if sample_times else 0.0
            required_reference_progress = max(5.0, min(15.0, probe_seconds * 0.25))
            reference_progress = (
                (max(reference_head_indices) - min(reference_head_indices)) / reference_fps
                if reference_fps > 0 and reference_head_indices else 0.0
            )
            reference_progress_ok = (
                reference_fps > 0
                and not stale_reference_seen
                and len(reference_head_indices) >= minimum_samples
                and sample_span >= max(5.0, probe_seconds * 0.5)
                and sample_times[-1] >= probe_seconds - 8.0
                and all(later > earlier for earlier, later in zip(sample_times, sample_times[1:]))
                and all(later - earlier <= 10.0 for earlier, later in zip(sample_times, sample_times[1:]))
                and all(later >= earlier for earlier, later in zip(
                    reference_head_indices, reference_head_indices[1:]
                ))
                and reference_progress >= required_reference_progress
                and reference_progress >= sample_span * 0.5
            )
            ok = (
                report.get("matchId") == match_id
                and bool(source_id)
                and inconsistent_distinct_samples == 0
                and len(delays) >= minimum_samples
                and max(upper_delays or [float("inf")]) <= max_delay
                and reference_progress_ok
            )
            serial_results[needed_phase] = gate(
                ok,
                sourceId=source_id,
                report=str(report_path.relative_to(root)) if report_path.is_relative_to(root) else str(report_path),
                rawSampleCount=len(target_samples),
                strongSampleCount=len(delays),
                rejectedDistinctSampleCount=inconsistent_distinct_samples,
                minimumStrongSamples=minimum_samples,
                medianAddedDelaySeconds=safe_median(delays),
                maxAddedDelaySeconds=max(delays) if delays else None,
                maxAddedDelayUpperBoundSeconds=round(max(upper_delays), 3) if upper_delays else None,
                maxAllowedSeconds=max_delay,
                strongSampleSpanSeconds=round(sample_span, 3),
                directReferenceProgressSeconds=round(reference_progress, 3),
                requiredReferenceProgressSeconds=round(required_reference_progress, 3),
                directReferenceFps=reference_fps or None,
                directReferenceProgressProven=reference_progress_ok,
                staleReferenceObserved=stale_reference_seen,
                strongSamples=[
                    {
                        "elapsedSeconds": row.get("elapsedSeconds"),
                        "addedDelaySeconds": row.get("addedDelaySeconds"),
                        "imageDifference": row.get("imageDifference"),
                        "alternativeMargin": row.get("alternativeMargin"),
                        "screenshotTimingSpanSeconds": row.get("screenshotTimingSpanSeconds"),
                    }
                    for row in strong
                ],
            )
            if not ok:
                failures.append(
                    f"{target_id}: {needed_phase} delay evidence is missing/weak/over "
                    f"{max_delay}s or direct reference did not advance"
                )

        if allowed_phases == {"initial", "final"}:
            source_consistent = bool(source_ids.get("initial")) and source_ids.get("initial") == source_ids.get("final")
            early = serial_results.get("initial", {})
            late = serial_results.get("final", {})
            early_median = early.get("medianAddedDelaySeconds")
            late_median = late.get("medianAddedDelaySeconds")
            growth = (
                round(float(late_median) - float(early_median), 3)
                if early_median is not None and late_median is not None else None
            )
            no_growth = growth is not None and growth <= max_growth
            serial_results["sourceAndAccumulation"] = gate(
                source_consistent and no_growth,
                sourceIdInitial=source_ids.get("initial"),
                sourceIdFinal=source_ids.get("final"),
                growthSeconds=growth,
                maxGrowthSeconds=max_growth,
            )
            if not source_consistent or not no_growth:
                failures.append(f"{target_id}: source changed or added delay accumulated beyond {max_growth}s")
        target_reports[target_id] = serial_results

    if not initial_timing_ok or not final_timing_ok:
        failures.append("delay probe timing does not cover the required playback window")
    delay_pass = not failures and initial_timing_ok and final_timing_ok and bool(target_reports)
    details["windowTiming"] = gate(
        initial_timing_ok and final_timing_ok,
        initialProbeWithinFiveMinutes=initial_timing_ok,
        finalProbeAfterRequiredDuration=final_timing_ok,
        requiredSeconds=required_seconds,
    )
    details["targets"] = target_reports
    details["pass"] = delay_pass
    details["policy"] = {
        "phase": phase,
        "maxAddedDelaySeconds": max_delay,
        "minimumStrongSamplesPerTargetPerWindow": minimum_samples,
        "maxAccumulationSeconds": max_growth,
        "strongMatchThresholds": {
            "imageDifferenceAtMost": 2.0,
            "alternativeMarginAtLeast": 3.0,
            "screenshotTimingSpanAtMostSeconds": 2.5,
            "referenceHeadIdleAtMostSeconds": 8.0,
        },
    }
    return details, [] if delay_pass else failures


def evaluate_source_identifiers(root: Path, targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare identifiers only; matching identifiers do not establish identical upstream URLs."""
    manifest = load_json(root / "delay-probes.json", {}) or {}
    probes = manifest.get("probes", []) if isinstance(manifest, dict) else []
    details = {}
    for target in targets:
        target_id = str(target.get("id"))
        log = read_text(evidence_dir(root, target) / "logcat.txt")
        sessions = [line for line in log.splitlines() if "startSession line=" in line]
        app_hashes = [re.search(r"\bsourceFingerprint=([0-9a-f]{64})\b", line) for line in sessions]
        upstream_events = [line for line in log.splitlines()
                           if "startSession line=" in line or "liveSourceRenewal " in line]
        app_upstream = [re.search(r"\bupstreamFingerprint=([0-9a-f]{64})\b", line)
                        for line in upstream_events]
        reference_hashes = []
        reference_upstream = []
        for phase in ("initial", "final"):
            matching = [p for p in probes if isinstance(p, dict) and p.get("phase") == phase
                        and str(p.get("matchId")) == str(target.get("matchId"))]
            if len(matching) != 1:
                reference_hashes.append(None)
                reference_upstream.append(None)
                continue
            report = load_json(root / str(matching[0].get("report", "")), {}) or {}
            source_id = str(report.get("sourceId") or "")
            fingerprint = report.get("sourceFingerprint")
            expected = hashlib.sha256(source_id.encode("utf-8")).hexdigest() if source_id else None
            reference_hashes.append(fingerprint if fingerprint == expected else None)
            upstream = report.get("upstreamFingerprint")
            reference_upstream.append(upstream if isinstance(upstream, str)
                                      and re.fullmatch(r"[0-9a-f]{64}", upstream) else None)
        app_values = [match.group(1) if match else None for match in app_hashes]
        app_upstream_values = [match.group(1) if match else None for match in app_upstream]
        ok = bool(app_values) and all(app_values) and all(reference_hashes) and len(set(app_values + reference_hashes)) == 1
        upstream_ok = (bool(app_upstream_values) and all(app_upstream_values) and all(reference_upstream)
                       and len(set(app_upstream_values + reference_upstream)) == 1)
        details[target_id] = gate(ok and upstream_ok, appFingerprints=app_values,
                                  referenceFingerprints=reference_hashes, upstreamPass=upstream_ok,
                                  appUpstreamFingerprints=app_upstream_values,
                                  referenceUpstreamFingerprints=reference_upstream)
    return gate(bool(details) and all(item["pass"] for item in details.values()), targets=details)


def evaluate(
    root: Path,
    phase: str = "all",
    required_minutes: int = 10,
    matches_per_variant: int = 3,
    max_delay: float = 5.0,
    minimum_delay_samples: int = 3,
    max_delay_growth: float = 0.0,
) -> dict[str, Any]:
    matrix = load_json(root / "matrix.json", {}) or {}
    orchestration = load_json(root / "orchestration.json", {}) or {}
    targets = [item for item in matrix.get("targets", []) if isinstance(item, dict)]
    failures: list[str] = []
    top_gates: dict[str, Any] = {}
    required_seconds = required_minutes * 60

    if phase == "all":
        status = str(orchestration.get("status", ""))
        orchestration_ok = status == "capture-completed" and not orchestration.get("error")
        top_gates["captureCompleted"] = gate(
            orchestration_ok,
            status=status,
            error=orchestration.get("error"),
            allTargetsReadyAt=orchestration.get("allTargetsReadyAt"),
            completedAt=orchestration.get("completedAt"),
        )
        if not orchestration_ok:
            failures.append("capture did not complete cleanly")

        ready_at = parse_time(orchestration.get("allTargetsReadyAt"))
        completed_at = parse_time(orchestration.get("completedAt"))
        common_seconds = (
            (completed_at - ready_at).total_seconds()
            if ready_at is not None and completed_at is not None and completed_at >= ready_at
            else 0
        )
        common_ok = common_seconds >= required_seconds
        top_gates["sameTimeWindow"] = gate(
            common_ok,
            commonWindowSeconds=round(common_seconds, 3),
            requiredSeconds=required_seconds,
        )
        if not common_ok:
            failures.append("the common capture window is shorter than the required continuous duration")

    identity_failures = [item for item in targets if not target_identity_ok(item)]
    unique_matches = {str(item.get("matchId")) for item in targets}
    matches_by_variant = {
        variant: {str(item.get("matchId")) for item in targets if item.get("variant") == variant}
        for variant in sorted(VARIANTS)
    }
    coverage_ok = (
        bool(targets)
        and not identity_failures
        and len(unique_matches) >= matches_per_variant
        and all(len(matches_by_variant[variant]) >= matches_per_variant for variant in VARIANTS)
        and len({str(item.get("serial")) for item in targets}) == len(targets)
    )
    top_gates["matrixCoverage"] = gate(
        coverage_ok,
        targetCount=len(targets),
        uniqueMatchIds=sorted(unique_matches),
        matchesByVariant={key: sorted(value) for key, value in matches_by_variant.items()},
        minimumMatchesPerVariant=matches_per_variant,
        uniqueSerialCount=len({str(item.get("serial")) for item in targets}),
    )
    if not coverage_ok:
        failures.append("target matrix does not prove three distinct matches on both TV and mobile")

    candidate_details, candidate_failures, candidates = candidate_gates(matrix)
    top_gates["candidateBuilds"] = gate(not candidate_failures, variants=candidate_details)
    failures.extend(candidate_failures)

    target_results: list[dict[str, Any]] = []
    if phase in {"early", "all"}:
        for target in targets:
            item, item_failures = evaluate_target(
                root,
                target,
                candidates,
                required_seconds=required_seconds,
                require_full_duration=phase == "all",
            )
            target_results.append(item)
            failures.extend(item_failures)
        if phase == "early":
            early_gate_keys = {
                "evidenceAndIdentity", "firstFrame", "processUnchanged",
                "noFatalOrAnr", "noPlaybackErrors", "rebufferZero",
                "renderedFrameProgress", "referenceSourceLine", "apkIdentity",
            }
            for item in target_results:
                item["pass"] = all(
                    item["gates"][key].get("pass") is True
                    for key in early_gate_keys if key in item["gates"]
                )
            failures = [failure for failure in failures if "duration is below" not in failure]

    delay_details, delay_failures = evaluate_delay(
        root,
        matrix,
        orchestration,
        phase=phase,
        max_delay=max_delay,
        minimum_samples=minimum_delay_samples,
        max_growth=max_delay_growth,
        required_seconds=required_seconds,
    )
    top_gates["delay"] = delay_details
    failures.extend(delay_failures)

    if phase == "all":
        source_identifiers = evaluate_source_identifiers(root, targets)
        top_gates["sourceIdentifiers"] = source_identifiers
        if not source_identifiers["pass"]:
            failures.append("app and reference source identifiers are missing or differ")
        # Index 0 in two separately fetched source lists is not proof that
        # they identify the same upstream. Nor do match IDs prove the picture.
        # Keep final acceptance closed until independent source identity and
        # visual fixture evidence are actually collected and evaluated.
        top_gates["sourceAndVisualIdentity"] = gate(
            False,
            reason="same-upstream identity and visual match identity are not yet verified",
        )
        failures.append("same-upstream and visual match identity evidence is not yet verified")

    if phase == "early":
        overall = (
            not orchestration.get("error")
            and coverage_ok
            and not candidate_failures
            and bool(target_results)
            and all(item.get("pass") is True for item in target_results)
        )
    elif phase in {"initial", "final"}:
        overall = coverage_ok and not candidate_failures and delay_details.get("pass") is True
    else:
        overall = (
            all(item.get("pass") is True for item in top_gates.values())
            and bool(target_results)
            and all(item.get("pass") is True for item in target_results)
            and not failures
        )

    unique_failures = list(dict.fromkeys(failures))
    return {
        "schemaVersion": 1,
        "phase": phase,
        "generatedAt": now_iso(),
        "overallPass": bool(overall),
        "requiredContinuousMinutes": required_minutes,
        "matchesPerVariant": matches_per_variant,
        "gates": top_gates,
        "targets": target_results,
        "failures": unique_failures,
        "limitations": [
            "Actual fixture identity and visible picture must still be reviewed from the saved screenshots; matchId arguments alone are not visual proof.",
            "The source-relative delay estimate uses arrival times of concurrently decoded upstream frames; weak/static image matches are rejected, not inferred.",
            "A pass applies only to the recorded device models, networks, candidate APK hashes, matches, and source lines in this evidence directory.",
        ],
    }


def make_self_test_fixture(root: Path) -> None:
    now = datetime.now(timezone.utc).astimezone()
    ready = now - timedelta(seconds=3705)
    completed = now
    matches = ["910001", "910002", "910003"]
    tv_sha = "a" * 64
    mobile_sha = "b" * 64
    source_backend = "c" * 64
    targets: list[dict[str, Any]] = []
    serial_index = 0
    delay_probes: list[dict[str, Any]] = []

    for variant, package, apk_sha in [
        ("tv", "com.qiubo.optimaltv.public", tv_sha),
        ("mobile", "com.qiubo.optimaltv.mobile.public", mobile_sha),
    ]:
        for match_id in matches:
            serial_index += 1
            serial = f"self-test-{serial_index}"
            target_id = f"{serial}-{match_id}"
            targets.append({
                "id": target_id,
                "serial": serial,
                "package": package,
                "variant": variant,
                "matchId": match_id,
                "label": f"self test {variant} {match_id}",
                "evidenceDirectory": f"runs/{target_id}",
                "expectedApkSha256": apk_sha,
            })

    matrix = {
        "schemaVersion": 1,
        "minimumContinuousMinutes": 60,
        "matchesPerVariant": 3,
        "candidates": {
            "tv": {
                "apkFileName": "self-tv.apk",
                "apkSha256": tv_sha,
                "embeddedBackendProxySha256": source_backend,
                "sourceProxySha256": source_backend,
                "backendMatchesSource": True,
            },
            "mobile": {
                "apkFileName": "self-mobile.apk",
                "apkSha256": mobile_sha,
                "embeddedBackendProxySha256": source_backend,
                "sourceProxySha256": source_backend,
                "backendMatchesSource": True,
            },
        },
        "targets": targets,
    }
    orchestration = {
        "status": "capture-completed",
        "error": None,
        "allTargetsReadyAt": ready.isoformat(),
        "completedAt": completed.isoformat(),
        "requiredContinuousSeconds": 3600,
    }
    write_json(root / "matrix.json", matrix)
    write_json(root / "orchestration.json", orchestration)

    for index, target in enumerate(targets):
        directory = root / target["evidenceDirectory"]
        directory.mkdir(parents=True)
        pid = str(4000 + index)
        run = {
            **{key: target[key] for key in ("serial", "package", "matchId")},
            "apkSha256": target["expectedApkSha256"],
            "startedAt": (ready - timedelta(seconds=30)).isoformat(),
            "requestedMinutes": 63,
        }
        result = {
            **{key: target[key] for key in ("serial", "package", "matchId")},
            "startPid": pid,
            "finalPid": pid,
            "observedPlaybackSeconds": 3650,
            "requestedMinutes": 63,
            "rebuffers": 0,
            "playbackErrors": 0,
            "crashes": 0,
            "backendMatched": True,
            "pass": True,
            "finishedAt": completed.isoformat(),
        }
        write_json(directory / "run.json", run)
        write_json(directory / "result.json", result)
        sample_rows = []
        for step in range(0, 3661, 30):
            at = ready + timedelta(seconds=step)
            rendered = step * 25
            sample_rows.append({
                "time": at.isoformat(),
                "elapsedSeconds": step + 30,
                "observedPlaybackSeconds": step,
                "firstFrameObserved": True,
                "pid": pid,
                "originalPid": pid,
                "processChanged": False,
                "matchId": target["matchId"],
                "package": target["package"],
                "serial": target["serial"],
                "rebuffers": 0,
                "playbackErrors": 0,
                "sourceFailures": 0,
                "crashes": 0,
                "heartbeat": {
                    "state": "READY",
                    "playing": True,
                    "renderedFrames": rendered,
                },
                "lastHeartbeat": f"hb READY playing=true rendered={rendered}",
            })
        (directory / "samples.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in sample_rows) + "\n",
            encoding="utf-8",
        )
        (directory / "logcat.txt").write_text(
            f"10-25 10:00:00.000 1 1 I OTV: startSession line=0 engine=media3 sourceFingerprint={hashlib.sha256(('source-' + target['matchId']).encode()).hexdigest()} upstreamFingerprint={'d' * 64}\n"
            "10-25 10:00:00.100 1 1 I OTV: [first_frame]\n"
            "10-25 10:00:10.000 1 1 I OTV: hb READY playing=true rendered=250\n",
            encoding="utf-8",
        )
        (directory / "first-frame.png").write_bytes(b"self-test-first-frame")
        (directory / "final-frame.png").write_bytes(b"self-test-final-frame")
        (directory / "crash-buffer.txt").write_text("", encoding="utf-8")
        (directory / "events.txt").write_text("", encoding="utf-8")
        (directory / "post-pid.txt").write_text(pid + "\n", encoding="utf-8")

    for needed_phase, started in [
        ("initial", ready + timedelta(seconds=10)),
        ("final", ready + timedelta(seconds=3610)),
    ]:
        finished = started + timedelta(seconds=45)
        for match_id in matches:
            match_targets = [item for item in targets if item["matchId"] == match_id]
            samples = []
            for target in match_targets:
                for sample_index in range(5):
                    head = 50 + sample_index * 40
                    frame_delay = 10
                    samples.append({
                        "serial": target["serial"],
                        "elapsedSeconds": 10 + sample_index * 8,
                        "addedDelaySeconds": frame_delay / 5,
                        "referenceHeadFrameIndex": head,
                        "referenceHeadFrameIndexAtScreenshotEnd": head + 1,
                        "matchedReferenceFrameIndex": head - frame_delay,
                        "referenceHeadIdleSeconds": 1.0,
                        "screenshotTimingSpanSeconds": 0.4,
                        "imageDifference": 0.8,
                        "alternativeMargin": 5.0,
                        "distinctMatch": True,
                    })
            report_rel = f"delay/{needed_phase}/{match_id}.json"
            write_json(root / report_rel, {
                "matchId": match_id,
                "sourceId": f"source-{match_id}",
                "sourceFingerprint": hashlib.sha256(f"source-{match_id}".encode()).hexdigest(),
                "upstreamFingerprint": "d" * 64,
                "referenceFrames": 211,
                "referenceFps": 5,
                "samples": samples,
                "limitations": ["self-test"],
            })
            delay_probes.append({
                "phase": needed_phase,
                "matchId": match_id,
                "startedAt": started.isoformat(),
                "finishedAt": finished.isoformat(),
                "report": report_rel,
                "serials": [item["serial"] for item in match_targets],
            })
    write_json(root / "delay-probes.json", {"schemaVersion": 1, "probes": delay_probes})


def mutate_and_expect_failure(base: Path, temp_root: Path, name: str, mutate: Any) -> None:
    case_root = temp_root / name
    shutil.copytree(base, case_root)
    mutate(case_root)
    summary = evaluate(case_root)
    baseline = evaluate(base)
    if summary["overallPass"] or set(summary["failures"]) <= set(baseline["failures"]):
        raise AssertionError(f"self-test mutation did not produce a new failure: {name}")


def self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="football-live-acceptance-") as temp:
        temp_root = Path(temp)
        base = temp_root / "base"
        base.mkdir()
        make_self_test_fixture(base)
        summary = evaluate(base)
        if summary["overallPass"] or summary["gates"]["sourceAndVisualIdentity"]["pass"]:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            raise AssertionError("unverified identity unexpectedly passed final acceptance")
        if not summary["gates"]["sourceIdentifiers"]["pass"]:
            raise AssertionError("matching source identifiers were not recognized")

        early_root = temp_root / "live-early"
        shutil.copytree(base, early_root)
        for target in load_json(early_root / "matrix.json", {})["targets"]:
            directory = early_root / target["evidenceDirectory"]
            (directory / "result.json").unlink(missing_ok=True)
            (directory / "post-pid.txt").unlink(missing_ok=True)
        early_summary = evaluate(early_root, phase="early")
        if not early_summary["overallPass"]:
            print(json.dumps(early_summary, ensure_ascii=False, indent=2))
            raise AssertionError("self-test live early fixture failed")

        def append_log(root: Path, line: str) -> None:
            target = load_json(root / "matrix.json", {})["targets"][0]
            path = root / target["evidenceDirectory"] / "logcat.txt"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

        mutate_and_expect_failure(
            base, temp_root, "rebuffer",
            lambda root: append_log(root, "10-25 10:01:00 I OTV: engine rebuffer #1"),
        )
        mutate_and_expect_failure(
            base, temp_root, "fatal",
            lambda root: append_log(root, "10-25 10:01:00 E AndroidRuntime: FATAL EXCEPTION: main"),
        )
        mutate_and_expect_failure(
            base, temp_root, "playback-error",
            lambda root: append_log(root, "10-25 10:01:00 E OTV: vlc error: EncounteredError"),
        )

        def source_switch(root: Path) -> None:
            target = load_json(root / "matrix.json", {})["targets"][0]
            path = root / target["evidenceDirectory"] / "logcat.txt"
            path.write_text(
                path.read_text(encoding="utf-8").replace("startSession line=0", "startSession line=1"),
                encoding="utf-8",
            )

        mutate_and_expect_failure(base, temp_root, "source-switch", source_switch)

        def mismatched_identifier(root: Path) -> None:
            target = load_json(root / "matrix.json", {})["targets"][0]
            path = root / target["evidenceDirectory"] / "logcat.txt"
            original = hashlib.sha256(f"source-{target['matchId']}".encode()).hexdigest()
            path.write_text(path.read_text(encoding="utf-8").replace(original, "f" * 64), encoding="utf-8")

        mutate_and_expect_failure(base, temp_root, "source-identifier-mismatch", mismatched_identifier)

        def missing_identifier(root: Path) -> None:
            target = load_json(root / "matrix.json", {})["targets"][0]
            path = root / target["evidenceDirectory"] / "logcat.txt"
            text = path.read_text(encoding="utf-8")
            path.write_text(re.sub(r" sourceFingerprint=[0-9a-f]{64}", "", text), encoding="utf-8")

        mutate_and_expect_failure(base, temp_root, "source-identifier-missing", missing_identifier)

        mutate_and_expect_failure(
            base, temp_root, "upstream-renewal-mismatch",
            lambda root: append_log(root, "10-25 10:30:00 I OTV: liveSourceRenewal upstreamFingerprint=" + "e" * 64),
        )
        mutate_and_expect_failure(
            base, temp_root, "upstream-renewal-unknown",
            lambda root: append_log(root, "10-25 10:30:00 I OTV: liveSourceRenewal upstreamFingerprint=unknown"),
        )

        def missing_first_frame(root: Path) -> None:
            target = load_json(root / "matrix.json", {})["targets"][0]
            (root / target["evidenceDirectory"] / "first-frame.png").unlink()

        mutate_and_expect_failure(base, temp_root, "missing-first-frame", missing_first_frame)

        def short_duration(root: Path) -> None:
            target = load_json(root / "matrix.json", {})["targets"][0]
            result_path = root / target["evidenceDirectory"] / "result.json"
            result = load_json(result_path, {})
            result["observedPlaybackSeconds"] = 599
            write_json(result_path, result)

        mutate_and_expect_failure(base, temp_root, "short-duration", short_duration)

        def process_change(root: Path) -> None:
            target = load_json(root / "matrix.json", {})["targets"][0]
            (root / target["evidenceDirectory"] / "post-pid.txt").write_text("999999\n", encoding="utf-8")

        mutate_and_expect_failure(base, temp_root, "process-change", process_change)

        def over_delay(root: Path) -> None:
            report_path = root / "delay/final/910001.json"
            report = load_json(report_path, {})
            report["samples"][0]["addedDelaySeconds"] = 5.5
            write_json(report_path, report)

        mutate_and_expect_failure(base, temp_root, "delay-over-five", over_delay)

        def accumulated_delay(root: Path) -> None:
            report_path = root / "delay/final/910001.json"
            report = load_json(report_path, {})
            for row in report["samples"]:
                row["matchedReferenceFrameIndex"] -= 1
                row["addedDelaySeconds"] += 0.2
            write_json(report_path, report)

        mutate_and_expect_failure(base, temp_root, "delay-accumulated", accumulated_delay)

        def weak_delay(root: Path) -> None:
            report_path = root / "delay/initial/910001.json"
            report = load_json(report_path, {})
            for row in report["samples"]:
                row["imageDifference"] = 8.0
                row["distinctMatch"] = False
            write_json(report_path, report)

        mutate_and_expect_failure(base, temp_root, "weak-delay", weak_delay)

        def early_final_probe(root: Path) -> None:
            manifest_path = root / "delay-probes.json"
            manifest = load_json(manifest_path, {})
            ready = parse_time(load_json(root / "orchestration.json", {})["allTargetsReadyAt"])
            for probe in manifest["probes"]:
                if probe["phase"] == "final" and probe["matchId"] == "910001":
                    probe["startedAt"] = (ready + timedelta(seconds=300)).isoformat()
                    probe["finishedAt"] = (ready + timedelta(seconds=345)).isoformat()
                    break
            write_json(manifest_path, manifest)

        mutate_and_expect_failure(base, temp_root, "final-probe-too-early", early_final_probe)

        print(json.dumps({
            "selfTestPass": True,
            "positiveFixture": "final identity gate correctly withheld",
            "negativeMutations": [
                "rebuffer", "fatal", "playback-error", "source-switch", "source-identifier-mismatch", "source-identifier-missing",
                "upstream-renewal-mismatch", "upstream-renewal-unknown",
                "missing-first-frame", "short-duration", "process-change",
                "delay-over-five", "delay-accumulated", "weak-delay", "final-probe-too-early",
            ],
        }, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--phase", choices=["early", "initial", "final", "all"], default="all")
    parser.add_argument("--required-minutes", type=int, default=10)
    parser.add_argument("--matches-per-variant", type=int, default=3)
    parser.add_argument("--max-delay-seconds", type=float, default=5.0)
    parser.add_argument("--minimum-delay-samples", type=int, default=3)
    parser.add_argument("--max-delay-growth-seconds", type=float, default=0.0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.root is None:
        parser.error("--root is required unless --self-test is used")
    if args.required_minutes < 10:
        parser.error("--required-minutes must be at least 10")
    if args.matches_per_variant < 3:
        parser.error("--matches-per-variant must be at least 3")
    if not math.isfinite(args.max_delay_seconds) or not 0 <= args.max_delay_seconds <= 5:
        parser.error("--max-delay-seconds must be between 0 and 5")
    if not math.isfinite(args.max_delay_growth_seconds) or args.max_delay_growth_seconds != 0:
        parser.error("--max-delay-growth-seconds must be 0 for no accumulation")
    if args.minimum_delay_samples < 3:
        parser.error("--minimum-delay-samples must be at least 3")

    summary = evaluate(
        root=args.root.resolve(),
        phase=args.phase,
        required_minutes=args.required_minutes,
        matches_per_variant=args.matches_per_variant,
        max_delay=args.max_delay_seconds,
        minimum_delay_samples=args.minimum_delay_samples,
        max_delay_growth=args.max_delay_growth_seconds,
    )
    output = args.output or (args.root / "summary.json")
    write_json(output.resolve(), summary)
    print(json.dumps({
        "phase": summary["phase"],
        "overallPass": summary["overallPass"],
        "failureCount": len(summary["failures"]),
        "output": str(output.resolve()),
    }, ensure_ascii=False))
    return 0 if summary["overallPass"] else 1


if __name__ == "__main__":
    sys.exit(main())

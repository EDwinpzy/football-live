"""Periodic playback-source health check with automatic disabling.

Why this exists: sources rot.  A station that answered yesterday can serve 404
lines today, and the admin panel speed test only tells you about the moment you
clicked it.  This runs the same device-side probe on a schedule and demotes
sources that stop working.

Two safety properties, both learned the hard way on 2026-09-30:

1. It disables, never deletes.  A macCMS source answers but rotates its m3u8
   links within hours, so a single bad reading must not destroy a source that
   may be fine tomorrow.  set_enabled(false) is reversible; delete is not.

2. Consecutive failures are required.  One bad round means nothing, because
   stations rate-limit and a shared probe path can blip.  Only N rounds in a
   row (default 3) demote a source, and a single success resets the counter.

The probe itself is delegated to the device-side relay (vod_probe_relay.py).
This module never touches the network directly: the self-hosted VPS has no
international egress, so measuring here would mark every Cloudflare source dead.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional


PROBE_TIMEOUT_SECONDS = 45.0
MIN_INTERVAL_SECONDS = 900.0
DEFAULT_FAILURES_REQUIRED = 3


@dataclass
class HealthVerdict:
    """One source outcome in a single round."""

    source_id: str
    name: str
    ok: bool
    latency_ms: Optional[int] = None
    lines: int = 0
    error: str = ""
    inconclusive: bool = False
    """True when the probe never reached the source (relay down, network blip).

    Such a round must not count against the source: it is a measurement
    failure, not a source failure.
    """


class SourceHealthRegistry:
    """Consecutive-failure bookkeeping that survives admin restarts."""

    def __init__(self, path, consecutive_failures_required=DEFAULT_FAILURES_REQUIRED):
        self._path = path
        self._required = max(1, int(consecutive_failures_required))
        self._state = {}
        self._lock = threading.Lock()

    @property
    def failures_required(self):
        return self._required

    def load(self):
        try:
            with open(self._path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        sources = payload.get("sources")
        if not isinstance(sources, dict):
            return
        with self._lock:
            self._state = {}
            for key, value in sources.items():
                row = value or {}
                self._state[str(key)] = {
                    "fails": int(row.get("fails") or 0),
                    "last_ok": row.get("last_ok"),
                }

    def save(self):
        with self._lock:
            snapshot = {"sources": dict(self._state)}
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def record(self, verdicts):
        """Update counters and return the subset that should be demoted now."""
        demoted = []
        now = time.time()
        with self._lock:
            for verdict in verdicts:
                entry = self._state.setdefault(verdict.source_id, {"fails": 0, "last_ok": None})
                if verdict.inconclusive:
                    continue
                if verdict.ok:
                    entry["fails"] = 0
                    entry["last_ok"] = now
                    continue
                entry["fails"] = int(entry.get("fails") or 0) + 1
                if entry["fails"] >= self._required:
                    demoted.append(verdict)
            live = set(v.source_id for v in verdicts)
            for gone in [k for k in list(self._state) if k not in live]:
                self._state.pop(gone, None)
        self.save()
        return demoted

    def failure_count(self, source_id):
        with self._lock:
            return int((self._state.get(source_id) or {}).get("fails") or 0)


def _relay_post(relay_url, path, payload, timeout):
    request = urllib.request.Request(
        relay_url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(256 * 1024).decode("utf-8", "replace"))


def probe_all(relay_url, sources, title):
    """Probe every source through the relay, serially.

    Serial on purpose: stations rate-limit, and a parallel sweep produced false
    failures on sources that answer fine when asked one at a time.
    """
    verdicts = []
    for source in sources:
        api_url = str(source.get("api_url") or "").strip()
        source_id = str(source.get("id") or "").strip()
        name = str(source.get("name") or source_id)
        if not api_url or not source_id:
            continue
        try:
            report = _relay_post(
                relay_url, "/probe-source",
                {"api_url": api_url, "title": title}, PROBE_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            detail = type(exc).__name__ + ": " + str(exc)
            verdicts.append(HealthVerdict(
                source_id=source_id, name=name, ok=False,
                error=detail[:80], inconclusive=True))
            continue
        latency = report.get("search_latency_ms")
        verdicts.append(HealthVerdict(
            source_id=source_id,
            name=name,
            ok=bool(report.get("ok")),
            latency_ms=int(latency) if isinstance(latency, (int, float)) else None,
            lines=int(report.get("lines") or 0),
            error=str(report.get("error") or "")[:120],
        ))
        time.sleep(1.0)
    return verdicts


class SourceHealthScheduler:
    """Run the health sweep on an interval and demote repeat offenders."""

    def __init__(
        self,
        runner: Callable[[], object],
        *,
        interval_seconds=6 * 3600.0,
        logger=print,
        run_immediately=False,
    ):
        if interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError("interval must be >= %d seconds" % MIN_INTERVAL_SECONDS)
        self._runner = runner
        self._interval = float(interval_seconds)
        self._logger = logger
        self._run_immediately = bool(run_immediately)
        self._stop = threading.Event()
        self._thread = None

    def run_forever(self):
        first = True
        while not self._stop.is_set():
            if first and not self._run_immediately:
                if self._stop.wait(self._interval):
                    return
            first = False
            try:
                self._runner()
            except Exception as exc:  # noqa: BLE001
                self._logger("[vod-health] round failed (%s: %s)" % (type(exc).__name__, str(exc)[:80]))

    def start(self):
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever, name="vod-source-health", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)


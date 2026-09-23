"""Daily IPTV rebuild scheduling and a shared, database-backed publish lock."""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from contextlib import contextmanager
from typing import Callable, Iterator
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


class PublishLease:
    """A live view of the publication lease, including heartbeat failure."""

    def __init__(self, lost: threading.Event) -> None:
        self._lost = lost

    def assert_valid(self) -> None:
        if self._lost.is_set():
            raise RuntimeError("IPTV publish lease was lost")


def next_daily_run(
    now: dt.datetime,
    hour: int = 6,
    minute: int = 30,
) -> dt.datetime:
    """Return the next local occurrence of the daily scheduled time."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("invalid scheduled time")
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    return candidate


class DatabasePublishLock:
    """Hold the IPTV publication lease through the stateless PostgREST API."""

    def __init__(
        self,
        rpc: Callable[[str, dict], object],
        *,
        heartbeat_seconds: float = 30.0,
        owner: str | None = None,
    ) -> None:
        self._rpc = rpc
        self._heartbeat_seconds = max(0.1, heartbeat_seconds)
        self._owner = owner or str(uuid.uuid4())
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None
        self._lost = threading.Event()

    def _request(self, action: str) -> dict:
        result = self._rpc(
            "iptv_admin_publish_lock",
            {"p_owner": self._owner, "p_action": action},
        )
        return result if isinstance(result, dict) else {}

    def _renew_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_seconds):
            try:
                if self._request("renew").get("locked") is not True:
                    self._lost.set()
                    return
            except Exception:  # noqa: BLE001 - fail closed if the lease cannot be renewed
                self._lost.set()
                return

    @contextmanager
    def hold(self) -> Iterator[PublishLease | None]:
        """Yield a live lease handle, or None if another publisher owns the lease."""
        acquired = self._request("acquire").get("locked") is True
        if not acquired:
            yield None
            return
        self._heartbeat = threading.Thread(
            target=self._renew_loop,
            name="iptv-publish-lock-heartbeat",
            daemon=True,
        )
        self._heartbeat.start()
        try:
            yield PublishLease(self._lost)
        finally:
            self._stop.set()
            self._heartbeat.join(timeout=min(self._heartbeat_seconds, 1.0))
            try:
                self._request("release")
            except Exception:  # noqa: BLE001 - the database lease expires if release fails
                pass


class DailyIptvScheduler:
    """Run the existing IPTV rebuild once per day at 06:30 Asia/Shanghai."""

    def __init__(
        self,
        runner: Callable[[], object],
        *,
        hour: int = 6,
        minute: int = 30,
        logger: Callable[[str], None] = print,
    ) -> None:
        self._runner = runner
        self._hour = hour
        self._minute = minute
        self._logger = logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_forever(self) -> None:
        while not self._stop.is_set():
            now = dt.datetime.now(SHANGHAI)
            when = next_daily_run(now, self._hour, self._minute)
            if self._stop.wait(max(0.0, (when - now).total_seconds())):
                return
            try:
                result = self._runner()
                succeeded = isinstance(result, dict) and result.get("error") is None
                status = "succeeded" if succeeded else "was skipped or failed"
                self._logger(f"IPTV daily rebuild {status}")
            except Exception as exc:  # noqa: BLE001 - keep the service alive for tomorrow
                self._logger(f"IPTV daily rebuild failed ({type(exc).__name__})")

    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever,
            name="iptv-daily-scheduler",
            daemon=True,
        )
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

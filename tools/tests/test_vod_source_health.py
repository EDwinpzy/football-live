"""Contract tests for scheduled playback-source health checking.

The important properties are the safety ones: a source is demoted only after
several consecutive bad rounds, a single good round clears the streak, an
unreachable probe counts for nothing, and demotion disables rather than
deletes.  A regression here silently disables working sources in production.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
HEALTH_PATH = ROOT / "tools" / "selfhosted" / "vod_source_health.py"


def load_health():
    spec = importlib.util.spec_from_file_location("vod_source_health", HEALTH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["vod_source_health"] = module
    spec.loader.exec_module(module)
    return module


health = load_health()


def verdict(sid, ok, **kw):
    return health.HealthVerdict(source_id=sid, name=sid, ok=ok, **kw)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.path = self.tmp.name

    def tearDown(self):
        Path(self.path).unlink(missing_ok=True)

    def test_one_bad_round_does_not_demote(self):
        reg = health.SourceHealthRegistry(self.path, consecutive_failures_required=3)
        self.assertEqual(reg.record([verdict("a", False)]), [])
        self.assertEqual(reg.failure_count("a"), 1)

    def test_demotes_only_after_consecutive_failures(self):
        reg = health.SourceHealthRegistry(self.path, consecutive_failures_required=3)
        reg.record([verdict("a", False)])
        reg.record([verdict("a", False)])
        demoted = reg.record([verdict("a", False)])
        self.assertEqual([v.source_id for v in demoted], ["a"])

    def test_a_single_success_resets_the_streak(self):
        reg = health.SourceHealthRegistry(self.path, consecutive_failures_required=3)
        reg.record([verdict("a", False)])
        reg.record([verdict("a", False)])
        reg.record([verdict("a", True)])
        self.assertEqual(reg.failure_count("a"), 0)
        self.assertEqual(reg.record([verdict("a", False)]), [])
        self.assertEqual(reg.record([verdict("a", False)]), [])
        self.assertEqual(len(reg.record([verdict("a", False)])), 1)

    def test_inconclusive_round_does_not_count(self):
        """A dead relay must never be read as N dead sources."""
        reg = health.SourceHealthRegistry(self.path, consecutive_failures_required=2)
        for _ in range(5):
            reg.record([verdict("a", False, inconclusive=True, error="relay down")])
        self.assertEqual(reg.failure_count("a"), 0)
        self.assertEqual(reg.record([verdict("a", False)]), [])

    def test_state_survives_a_restart(self):
        reg = health.SourceHealthRegistry(self.path, consecutive_failures_required=3)
        reg.record([verdict("a", False)])
        reg.record([verdict("a", False)])
        again = health.SourceHealthRegistry(self.path, consecutive_failures_required=3)
        again.load()
        self.assertEqual(again.failure_count("a"), 2)
        self.assertEqual(len(again.record([verdict("a", False)])), 1)

    def test_removed_sources_are_forgotten(self):
        reg = health.SourceHealthRegistry(self.path, consecutive_failures_required=5)
        reg.record([verdict("gone", False)])
        self.assertEqual(reg.failure_count("gone"), 1)
        reg.record([verdict("kept", True)])
        self.assertEqual(reg.failure_count("gone"), 0)


class ProbeAllTests(unittest.TestCase):
    SOURCES = [
        {"id": "a", "name": "A", "api_url": "https://a.example/api.php/provide/vod/"},
        {"id": "b", "name": "B", "api_url": "https://b.example/api.php/provide/vod/"},
    ]

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.path = self.tmp.name

    def tearDown(self):
        Path(self.path).unlink(missing_ok=True)

    def test_healthy_source_reports_latency_and_lines(self):
        report = {"ok": True, "search_latency_ms": 420, "lines": 46, "error": ""}
        with mock.patch.object(health, "_relay_post", return_value=report):
            with mock.patch.object(health.time, "sleep"):
                verdicts = health.probe_all("http://relay", self.SOURCES, "Q")
        self.assertTrue(all(v.ok for v in verdicts))
        self.assertEqual(verdicts[0].latency_ms, 420)
        self.assertEqual(verdicts[0].lines, 46)

    def test_a_relay_outage_marks_everything_inconclusive(self):
        with mock.patch.object(health, "_relay_post", side_effect=OSError("refused")):
            with mock.patch.object(health.time, "sleep"):
                verdicts = health.probe_all("http://relay", self.SOURCES, "Q")
        self.assertTrue(all(v.inconclusive for v in verdicts))
        self.assertTrue(all(not v.ok for v in verdicts))

    def test_one_failing_source_does_not_stop_the_sweep(self):
        def side(url, path, payload, timeout):
            if "b.example" in payload["api_url"]:
                raise OSError("boom")
            return {"ok": True, "search_latency_ms": 10, "lines": 1, "error": ""}

        with mock.patch.object(health, "_relay_post", side_effect=side):
            with mock.patch.object(health.time, "sleep"):
                verdicts = health.probe_all("http://relay", self.SOURCES, "Q")
        by_id = {v.source_id: v for v in verdicts}
        self.assertTrue(by_id["a"].ok)
        self.assertTrue(by_id["b"].inconclusive)

    def test_a_successful_search_without_this_title_is_inconclusive(self):
        report = {
            "ok": False,
            "search_latency_ms": 420,
            "lines": 0,
            "probed": 0,
            "error": "no playable line in search result",
        }
        with mock.patch.object(health, "_relay_post", return_value=report):
            with mock.patch.object(health.time, "sleep"):
                verdicts = health.probe_all("http://relay", self.SOURCES[:1], "anime title")
        self.assertFalse(verdicts[0].ok)
        self.assertTrue(verdicts[0].inconclusive)
        self.assertEqual(verdicts[0].lines, 0)

    def test_repeated_empty_title_searches_do_not_demote_a_source(self):
        report = {
            "ok": False,
            "search_latency_ms": 420,
            "lines": 0,
            "error": "no playable line in search result",
        }
        reg = health.SourceHealthRegistry(self.path, consecutive_failures_required=2)
        with mock.patch.object(health, "_relay_post", return_value=report):
            with mock.patch.object(health.time, "sleep"):
                for _ in range(4):
                    verdicts = health.probe_all("http://relay", self.SOURCES[:1], "anime title")
                    self.assertEqual(reg.record(verdicts), [])
        self.assertEqual(reg.failure_count("a"), 0)

    def test_real_search_http_error_is_still_counted(self):
        report = {"ok": False, "search_latency_ms": None, "lines": 0, "error": "HTTP 403"}
        reg = health.SourceHealthRegistry(self.path, consecutive_failures_required=3)
        with mock.patch.object(health, "_relay_post", return_value=report):
            with mock.patch.object(health.time, "sleep"):
                verdicts = health.probe_all("http://relay", self.SOURCES[:1], "anime title")
        self.assertFalse(verdicts[0].inconclusive)
        self.assertEqual(reg.record(verdicts), [])
        self.assertEqual(reg.failure_count("a"), 1)

    def test_relay_allowlist_refusal_is_inconclusive_and_not_counted(self):
        report = {
            "ok": False,
            "search_latency_ms": 420,
            "lines": 2,
            "probed": 0,
            "inconclusive": True,
            "error": "all sampled stream URLs rejected before HTTP probe",
        }
        reg = health.SourceHealthRegistry(self.path, consecutive_failures_required=2)
        with mock.patch.object(health, "_relay_post", return_value=report):
            with mock.patch.object(health.time, "sleep"):
                for _ in range(3):
                    verdicts = health.probe_all("http://relay", self.SOURCES[:1], "anime title")
                    self.assertTrue(verdicts[0].inconclusive)
                    self.assertEqual(reg.record(verdicts), [])
        self.assertEqual(reg.failure_count("a"), 0)

    def test_real_failed_playback_lines_still_accumulate_to_demotion(self):
        report = {
            "ok": False,
            "search_latency_ms": 420,
            "lines": 8,
            "probed": 8,
            "error": "interface ok, 8 lines probed, none played",
        }
        reg = health.SourceHealthRegistry(self.path, consecutive_failures_required=3)
        with mock.patch.object(health, "_relay_post", return_value=report):
            with mock.patch.object(health.time, "sleep"):
                demotions = []
                for _ in range(3):
                    verdicts = health.probe_all("http://relay", self.SOURCES[:1], "anime title")
                    demotions = reg.record(verdicts)
        self.assertFalse(verdicts[0].inconclusive)
        self.assertEqual(reg.failure_count("a"), 3)
        self.assertEqual([v.source_id for v in demotions], ["a"])

    def test_sources_without_api_url_are_skipped(self):
        sources = [{"id": "x", "name": "X", "api_url": ""}, {"name": "no id"}]
        with mock.patch.object(health, "_relay_post") as post:
            with mock.patch.object(health.time, "sleep"):
                verdicts = health.probe_all("http://relay", sources, "Q")
        self.assertEqual(verdicts, [])
        post.assert_not_called()


class SchedulerTests(unittest.TestCase):
    def test_rejects_a_too_short_interval(self):
        with self.assertRaises(ValueError):
            health.SourceHealthScheduler(lambda: None, interval_seconds=60)

    def test_minimum_interval_is_fifteen_minutes(self):
        self.assertEqual(health.MIN_INTERVAL_SECONDS, 900.0)
        health.SourceHealthScheduler(lambda: None, interval_seconds=900)

    def test_a_failing_round_does_not_stop_the_loop(self):
        calls = []

        def runner():
            calls.append(1)
            raise RuntimeError("probe blew up")

        sched = health.SourceHealthScheduler(runner, interval_seconds=900, run_immediately=True)
        # Patch the instance's own event: patching threading.Event after __init__
        # would leave the real (900s) event in place and hang the suite.
        fake = mock.Mock()
        fake.is_set.side_effect = [False, False, True]
        fake.wait.return_value = False
        sched._stop = fake
        sched.run_forever()
        self.assertGreaterEqual(len(calls), 2)



"""竞速早返回 + 源质量画像 + 熔断契约（2026-09-19）。

锁住三条线上口径：
1) 早返回：≥2 台源出有效线路即进探测、首个可播 + 短宽限即返回，
   慢源不再把整批拖到自己的超时（旧实现 P50 实测 ~7.5s）。
2) 画像：搜索成功率/EMA 延迟决定发车顺序；连续失败熔断进冷却，
   冷却期间跳过、到期放行试探；全部熔断时忽略熔断器照常发车（宁可慢不能没片看）。
3) 后台补完：早返回时被甩下的源后台跑完，合并进短 TTL 缓存，
   同片再查给全量且不重复触网。
"""
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import vod_sources  # noqa: E402


SUBJECT = {"title": "肖申克的救赎", "year": "1994", "category": "movie", "season": 0}
PROBE_OK = {"alive": True, "latency_ms": 100, "height": 1080}


def _source(source_id, adapter_type="macms_json"):
    return vod_sources.VodSource(
        id=source_id, name=source_id, site_url="https://example.test",
        api_url="https://example.test/api.php/provide/vod/",
        adapter_type=adapter_type, enabled=True, revision=1)


class FakeAdapter:
    """按 source.id 查表决定行为：fast=秒回出线，slow=睡 3 秒，dead=抛异常。"""

    behaviors = {}
    calls = {}

    def __init__(self, source):
        self.source = source

    def search(self, subject):
        FakeAdapter.calls[self.source.id] = FakeAdapter.calls.get(self.source.id, 0) + 1
        behavior = FakeAdapter.behaviors.get(self.source.id, "fast")
        if behavior == "dead":
            raise RuntimeError("source down")
        if behavior == "slow":
            time.sleep(3)
        return [{"title": subject["title"], "year": subject.get("year"),
                 "category": subject.get("category"), "season": 0}]

    def detail(self, candidate):
        return [{"id": "%s:1" % self.source.id, "source_id": self.source.id,
                 "name": "线", "height": 0, "latency_ms": None, "alive": True,
                 "episodes": [{"index": 0, "name": "第1集", "url": "https://v.example/1.m3u8"}]}]

    def resolve(self, line, episode_index):
        return {"line_id": line.get("id"), "name": "第1集",
                "url": "https://v.example/1.m3u8", "headers": {}}


class RacingTestCase(unittest.TestCase):
    def setUp(self):
        self._real_macms = vod_sources.MacCmsAdapter
        self._grace = vod_sources.PROBE_EARLY_GRACE
        vod_sources.MacCmsAdapter = FakeAdapter
        vod_sources.PROBE_EARLY_GRACE = 0.2
        FakeAdapter.behaviors = {}
        FakeAdapter.calls = {}
        self.addCleanup(self._restore)

    def _restore(self):
        vod_sources.MacCmsAdapter = self._real_macms
        vod_sources.PROBE_EARLY_GRACE = self._grace

    def _registry(self, ids, stats=None):
        return vod_sources.SourceRegistry(
            [_source(i) for i in ids], version=1, stats=stats, probe=lambda _url: dict(PROBE_OK))


class EarlyReturnTest(RacingTestCase):
    def test_two_fast_sources_return_before_slow_source_finishes(self):
        FakeAdapter.behaviors = {"s1": "fast", "s2": "fast", "s3": "slow"}
        registry = self._registry(["s1", "s2", "s3"])
        started = time.monotonic()
        lines = registry.resolve_subject(dict(SUBJECT))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)            # 旧实现要等 s3 的 3 秒
        self.assertEqual({line["source_id"] for line in lines}, {"s1", "s2"})

    def test_background_completion_merges_slow_source_into_cache(self):
        FakeAdapter.behaviors = {"s1": "fast", "s2": "fast", "s3": "slow"}
        registry = self._registry(["s1", "s2", "s3"])
        first = registry.resolve_subject(dict(SUBJECT))
        self.assertEqual({line["source_id"] for line in first}, {"s1", "s2"})
        # 后台补完线程把慢源结果合进缓存（缓存键与 subject 对应）
        deadline = time.monotonic() + 6
        cached = None
        while time.monotonic() < deadline:
            cached = registry._cache_get(vod_sources._subject_cache_key(SUBJECT))
            if cached and {line["source_id"] for line in cached} == {"s1", "s2", "s3"}:
                break
            time.sleep(0.1)
        self.assertIsNotNone(cached)
        self.assertEqual({line["source_id"] for line in cached}, {"s1", "s2", "s3"})
        # 再查同一片：缓存命中给全量，且不重复触网
        calls_before = dict(FakeAdapter.calls)
        second = registry.resolve_subject(dict(SUBJECT))
        self.assertEqual(FakeAdapter.calls, calls_before)
        self.assertEqual({line["source_id"] for line in second}, {"s1", "s2", "s3"})

    def test_total_budget_caps_when_no_source_answers_in_time(self):
        budget = vod_sources.RESOLVE_TOTAL_BUDGET
        vod_sources.RESOLVE_TOTAL_BUDGET = 1.0
        self.addCleanup(setattr, vod_sources, "RESOLVE_TOTAL_BUDGET", budget)
        FakeAdapter.behaviors = {"s1": "slow", "s2": "slow"}
        registry = self._registry(["s1", "s2"])
        started = time.monotonic()
        lines = registry.resolve_subject(dict(SUBJECT))
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(lines, [])

    def test_cache_hit_skips_search_entirely(self):
        registry = self._registry(["s1", "s2"])
        first = registry.resolve_subject(dict(SUBJECT))
        self.assertTrue(first)
        calls_before = dict(FakeAdapter.calls)
        second = registry.resolve_subject(dict(SUBJECT))
        self.assertEqual(FakeAdapter.calls, calls_before)
        self.assertEqual([line["id"] for line in second], [line["id"] for line in first])


class SourceStatsTest(unittest.TestCase):
    def test_breaker_opens_after_consecutive_failures_and_expires(self):
        stats = vod_sources.SourceStats()
        now = time.time()
        self.assertFalse(stats.is_circuit_open("s1", now))
        for _ in range(vod_sources.BREAKER_CONSEC_FAILS):
            stats.record_search("s1", False, None)
        self.assertTrue(stats.is_circuit_open("s1", now + 1))
        self.assertFalse(stats.is_circuit_open(
            "s1", now + 1 + max(60, vod_sources.BREAKER_COOLDOWN_SECONDS)))
        stats.record_search("s1", True, 800)     # 试探成功即复位
        self.assertFalse(stats.is_circuit_open("s1"))
        self.assertEqual(stats.data["s1"]["consec_fail"], 0)

    def test_score_prefers_reliable_and_fast_sources(self):
        stats = vod_sources.SourceStats()
        for _ in range(8):
            stats.record_search("good", True, 500)
        for _ in range(8):
            stats.record_search("bad", False, None)
        stats.record_search("meh", True, 9000)
        ordered = [s.id for s in stats.sort_sources(
            [_source("bad"), _source("meh"), _source("good"), _source("new")])]
        self.assertEqual(ordered[0], "good")
        self.assertEqual(ordered[-1], "bad")

    def test_ema_moves_towards_new_latency_samples(self):
        stats = vod_sources.SourceStats()
        stats.record_search("s1", True, 1000)
        first = stats.data["s1"]["ema_ms"]
        stats.record_search("s1", True, 3000)
        second = stats.data["s1"]["ema_ms"]
        self.assertEqual(first, 1000)
        self.assertGreater(second, first)
        self.assertLess(second, 3000)

    def test_flush_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "stats.json"
            stats = vod_sources.SourceStats(path)
            stats.record_search("s1", True, 700)
            stats.flush()
            reloaded = vod_sources.SourceStats(path)
            self.assertEqual(reloaded.data["s1"]["ok"], 1)
            self.assertEqual(reloaded.data["s1"]["ema_ms"], 700)


class BreakerRoutingTest(RacingTestCase):
    def test_circuit_open_source_is_skipped(self):
        stats = vod_sources.SourceStats()
        for _ in range(vod_sources.BREAKER_CONSEC_FAILS):
            stats.record_search("s3", False, None)
        FakeAdapter.behaviors = {"s1": "fast", "s2": "fast"}
        registry = self._registry(["s1", "s2", "s3"], stats=stats)
        lines = registry.resolve_subject(dict(SUBJECT))
        self.assertTrue(lines)
        self.assertNotIn("s3", FakeAdapter.calls)   # 熔断期根本没发车

    def test_all_sources_open_falls_back_to_full_fleet(self):
        stats = vod_sources.SourceStats()
        for source_id in ("s1", "s2"):
            for _ in range(vod_sources.BREAKER_CONSEC_FAILS):
                stats.record_search(source_id, False, None)
        FakeAdapter.behaviors = {"s1": "fast", "s2": "fast"}
        registry = self._registry(["s1", "s2"], stats=stats)
        lines = registry.resolve_subject(dict(SUBJECT))
        self.assertEqual({line["source_id"] for line in lines}, {"s1", "s2"})


class FastResolverPoolTest(unittest.TestCase):
    def test_title_match_score_is_added_separately_from_line_quality_score(self):
        source = _source("s1")
        registry = vod_sources.SourceRegistry([source])
        original = vod_sources.MacCmsAdapter

        class MatchedAdapter:
            def __init__(self, _source):
                pass

            def search(self, subject):
                return [{"id": "content-1", "title": subject["title"],
                         "year": "1994", "category": "movie", "season": 0}]

            def detail(self, _candidate):
                return [{"id": "s1:content-1", "source_id": "s1", "source_name": "s1",
                         "score": 97, "episodes": [{"index": 0, "name": "Episode 1",
                                                      "url": "https://media.example/1.m3u8"}]}]

        vod_sources.MacCmsAdapter = MatchedAdapter
        try:
            _source_obj, lines, error, _elapsed = registry._search_one(
                source, {"title": "肖申克的救赎", "year": "1994", "category": "movie", "season": 0})
        finally:
            vod_sources.MacCmsAdapter = original

        self.assertIsNone(error)
        self.assertEqual(lines[0]["match_score"], 100)
        self.assertEqual(lines[0]["score"], 97)

    def test_health_order_is_cached_for_five_minutes(self):
        class RecordingStats:
            def __init__(self):
                self.sort_calls = 0

            def is_circuit_open(self, _source_id, now=None):
                return False

            def sort_sources(self, sources):
                self.sort_calls += 1
                return list(reversed(sources))

        now = [10.0]
        stats = RecordingStats()
        registry = vod_sources.SourceRegistry(
            [_source("s1"), _source("s2"), _source("s3")],
            stats=stats, clock=lambda: now[0])

        first = registry.resolver_source_order()
        second = registry.resolver_source_order()
        now[0] += 301
        third = registry.resolver_source_order()

        self.assertEqual([item.id for item in first], ["s3", "s2", "s1"])
        self.assertEqual([item.id for item in second], ["s3", "s2", "s1"])
        self.assertEqual([item.id for item in third], ["s3", "s2", "s1"])
        self.assertEqual(stats.sort_calls, 2)

    def test_open_circuit_is_filtered_fresh_without_rebuilding_health_order(self):
        class RecordingStats:
            def __init__(self):
                self.sort_calls = 0
                self.open_source = None

            def is_circuit_open(self, source_id, now=None):
                return source_id == self.open_source

            def sort_sources(self, sources):
                self.sort_calls += 1
                return list(reversed(sources))

        stats = RecordingStats()
        registry = vod_sources.SourceRegistry(
            [_source("s1"), _source("s2"), _source("s3")], stats=stats)
        first = registry.resolver_source_order()
        stats.open_source = "s2"
        during_cooldown = registry.resolver_source_order()
        stats.open_source = None
        after_cooldown = registry.resolver_source_order()

        self.assertEqual([item.id for item in first], ["s3", "s2", "s1"])
        self.assertEqual([item.id for item in during_cooldown], ["s3", "s1"])
        self.assertEqual([item.id for item in after_cooldown], ["s3", "s2", "s1"])
        self.assertEqual(stats.sort_calls, 1)

    def test_close_cancels_queued_bounded_resolver_searches(self):
        registry = vod_sources.SourceRegistry(
            [_source("s%d" % index) for index in range(6)])
        release = threading.Event()
        started = {"s%d" % index: threading.Event() for index in range(6)}

        def blocked_search(source, _subject):
            started[source.id].set()
            release.wait()
            return source, [], None, 100

        registry._search_one = blocked_search
        try:
            active = registry.submit_fast_searches(dict(SUBJECT))
            self.assertTrue(all(started["s%d" % index].wait(1) for index in range(3)))
            queued = registry.submit_fast_searches(dict(SUBJECT))
            registry.close()

            self.assertEqual(len(active), 3)
            self.assertEqual(len(queued), 3)
            self.assertTrue(all(future.cancelled() for future in queued))
        finally:
            release.set()
            close = getattr(registry, "close", None)
            if close:
                close()
            for future in locals().get("active", ()):
                try:
                    future.result(timeout=1)
                except concurrent.futures.CancelledError:
                    pass

    def test_timed_out_searches_self_release_bounded_pool_slots(self):
        delay_seconds = 0.3

        class SlowResponseHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(delay_seconds)
                body = b'{"code":1,"list":[]}'
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # The client has already timed out and closed its socket.

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowResponseHandler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        api_url = "http://127.0.0.1:%d/api.php/provide/vod/" % server.server_port
        registry = vod_sources.SourceRegistry(
            [vod_sources.VodSource(
                "s%d" % index, "s%d" % index, "http://127.0.0.1", api_url,
                "macms_json") for index in range(6)])
        try:
            with patch.object(vod_sources, "FAST_SOURCE_JOB_TIMEOUT_SECONDS", 0.08, create=True), \
                    patch.object(vod_sources, "_validate_public_url",
                                 side_effect=lambda url, resolve_dns=False: url):
                first = registry.submit_fast_searches(dict(SUBJECT))
                first_results = [future.result(timeout=2) for future in first]

                self.assertEqual(len(first), 3)
                self.assertTrue(all(result[2] is not None for result in first_results))
                self.assertLess(max(result[3] for result in first_results), 500)

                callbacks_finished = threading.Event()
                remaining_callbacks = [len(first)]
                callback_lock = threading.Lock()

                def note_slot_released(_future):
                    with callback_lock:
                        remaining_callbacks[0] -= 1
                        if remaining_callbacks[0] == 0:
                            callbacks_finished.set()

                for future in first:
                    future.add_done_callback(note_slot_released)
                self.assertTrue(callbacks_finished.wait(2))

                recovered = registry.submit_fast_searches(dict(SUBJECT))
                self.assertEqual(len(recovered), 3)
                recovered_results = [future.result(timeout=2) for future in recovered]
                self.assertTrue(all(result[2] is not None for result in recovered_results))
        finally:
            registry.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1)

    def test_hhkan_fast_search_passes_timeout_and_body_limit_to_fetch(self):
        import hhkan

        source = vod_sources.VodSource(
            "hhkan-test", "hhkan-test", "https://hhkan.example",
            "https://hhkan.example/api", "hhkan")
        registry = vod_sources.SourceRegistry([source])
        observed = []
        token_observed = []

        def slow_page(path, timeout=None, max_bytes=None):
            observed.append((path, timeout, max_bytes))
            time.sleep(0.02)
            raise TimeoutError("source request timed out")

        def search_token(timeout=None, max_bytes=None):
            token_observed.append((timeout, max_bytes))
            return "test-token"

        with patch.object(vod_sources, "FAST_SOURCE_JOB_TIMEOUT_SECONDS", 0.1, create=True), \
                patch.object(hhkan, "get_page", side_effect=slow_page), \
                patch.object(hhkan, "get_search_token", side_effect=search_token):
            try:
                futures = registry.submit_fast_searches(dict(SUBJECT))
                _source_result, _lines, error, elapsed_ms = futures[0].result(timeout=1)
            finally:
                registry.close()

        self.assertIsNotNone(error)
        self.assertLess(elapsed_ms, 500)
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0][1], 0)
        self.assertLessEqual(observed[0][1], 0.1)
        self.assertGreater(observed[0][2], 0)
        self.assertEqual(len(token_observed), 1)
        self.assertGreater(token_observed[0][0], 0)
        self.assertLessEqual(token_observed[0][0], 0.1)
        self.assertGreater(token_observed[0][1], 0)


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

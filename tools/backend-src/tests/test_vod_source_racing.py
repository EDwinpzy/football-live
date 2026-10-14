"""竞速早返回 + 源质量画像 + 熔断契约（2026-09-19）。

锁住三条线上口径：
1) 早返回：≥2 台源出有效线路即进探测、首个可播 + 短宽限即返回，
   慢源不再把整批拖到自己的超时（旧实现 P50 实测 ~7.5s）。
2) 画像：搜索成功率/EMA 延迟决定发车顺序；连续失败熔断进冷却，
   冷却期间跳过、到期放行试探；全部熔断时忽略熔断器照常发车（宁可慢不能没片看）。
3) 后台补完：早返回时被甩下的源后台跑完，合并进短 TTL 缓存，
   同片再查给全量且不重复触网。
4) 首条可播快速路径（2026-10-14）：详情页按「谁先出实测可播线路」收队，
   快源给的死链不得把详情页钉死；未探测线路保持 alive=None，不冒充可播。
5) 落盘缓存：终态结果落盘，冷启动后的新实例直接命中、不触网。
"""
import json
import pathlib
import socket
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
import source_dns  # noqa: E402


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
    line_counts = {}

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
        count = max(1, int(FakeAdapter.line_counts.get(self.source.id, 1)))
        return [{"id": "%s:1:%d" % (self.source.id, index), "source_id": self.source.id,
                 "source_name": self.source.id, "name": "线%d" % (index + 1),
                 "height": 0, "latency_ms": None, "alive": None,
                 "episodes": [{"index": 0, "name": "第1集",
                               "url": "https://v.example/%s/%d.m3u8" % (self.source.id, index)}]}
                for index in range(count)]

    def resolve(self, line, episode_index):
        return {"line_id": line.get("id"), "name": "第1集",
                "url": (line.get("episodes") or [{}])[0].get("url") or
                       "https://v.example/%s/0.m3u8" % line.get("source_id"),
                "headers": {}}


class RacingTestCase(unittest.TestCase):
    def setUp(self):
        self._real_macms = vod_sources.MacCmsAdapter
        self._grace = vod_sources.PROBE_EARLY_GRACE
        vod_sources.MacCmsAdapter = FakeAdapter
        vod_sources.PROBE_EARLY_GRACE = 0.2
        FakeAdapter.behaviors = {}
        FakeAdapter.calls = {}
        FakeAdapter.line_counts = {}
        self.addCleanup(self._restore)

    def _restore(self):
        vod_sources.MacCmsAdapter = self._real_macms
        vod_sources.PROBE_EARLY_GRACE = self._grace

    def _registry(self, ids, stats=None, alive_sources=None):
        """``alive_sources`` 为 None 时所有线路探测都通过；否则只有名单内源的线路可播。"""

        def probe(url):
            if alive_sources is None:
                return dict(PROBE_OK)
            for source_id in alive_sources:
                if "/%s/" % source_id in str(url):
                    return dict(PROBE_OK)
            return {"alive": False, "latency_ms": None, "height": 0}

        return vod_sources.SourceRegistry(
            [_source(i) for i in ids], version=1, stats=stats, probe=probe)


class FirstPlayableTest(RacingTestCase):
    """详情页快速路径契约（2026-10-14）。

    线上回归现场：非凡资源 0.55s 出 2 条线路、好好看 2.30s 出 33 条。旧策略
    「谁先出线就用谁、不探测」在 0.55s 返回那 2 条，而它们实际不可播 → 详情页
    只有 2 条死线。这里锁住新口径：**谁先出「实测可播」的线路就用谁**。
    """

    def test_verified_line_returns_without_waiting_for_the_slow_source(self):
        FakeAdapter.behaviors = {"s1": "fast", "s2": "slow"}
        registry = self._registry(["s1", "s2"], alive_sources=["s1"])

        started = time.monotonic()
        lines = registry.resolve_subject(dict(SUBJECT), first_playable=True)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5, "首源可播就不该等慢源")
        self.assertEqual({line["source_id"] for line in lines}, {"s1"})
        self.assertTrue(all(line.get("alive") is True for line in lines))

    def test_dead_fast_source_does_not_pin_the_detail_page(self):
        """回归锁：快源出线但线路全死 → 必须继续等下一台源出可播线路。"""
        FakeAdapter.behaviors = {"s1": "fast", "s2": "slow"}
        registry = self._registry(["s1", "s2"], alive_sources=["s2"])

        started = time.monotonic()
        lines = registry.resolve_subject(dict(SUBJECT), first_playable=True)
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 2.0, "慢源还没出线就不该收队")
        self.assertEqual({line["source_id"] for line in lines}, {"s1", "s2"})
        verified = [line for line in lines if line.get("alive") is True]
        self.assertEqual([line["source_id"] for line in verified], ["s2"])
        self.assertEqual(lines[0]["source_id"], "s2", "实测可播的线路必须排最前")

    def test_unprobed_lines_stay_unknown_instead_of_claiming_playability(self):
        FakeAdapter.behaviors = {"s1": "fast", "s2": "slow"}
        FakeAdapter.line_counts = {"s1": 6}
        registry = self._registry(["s1", "s2"], alive_sources=["s2"])

        lines = registry.resolve_subject(dict(SUBJECT), first_playable=True)
        fast = [line.get("alive") for line in lines if line["source_id"] == "s1"]

        self.assertEqual(len(fast), 6)
        self.assertEqual(fast.count(False), vod_sources.FIRST_PLAYABLE_PROBES_PER_SOURCE)
        self.assertEqual(fast.count(None), 6 - vod_sources.FIRST_PLAYABLE_PROBES_PER_SOURCE)
        self.assertNotIn(True, fast, "没探测过的线路不能冒充可播")

    def test_dead_everything_returns_collected_lines_without_claiming_playability(self):
        FakeAdapter.behaviors = {"s1": "fast", "s2": "fast"}
        registry = self._registry(["s1", "s2"], alive_sources=[])

        started = time.monotonic()
        lines = registry.resolve_subject(dict(SUBJECT), first_playable=True)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, vod_sources.FIRST_PLAYABLE_BUDGET_SECONDS + 1.5)
        self.assertEqual({line["source_id"] for line in lines}, {"s1", "s2"})
        self.assertNotIn(True, [line.get("alive") for line in lines])

    def test_slow_source_still_completes_into_the_cache_for_a_later_open(self):
        FakeAdapter.behaviors = {"s1": "fast", "s2": "slow"}
        registry = self._registry(["s1", "s2"], alive_sources=["s1"])
        first = registry.resolve_subject(dict(SUBJECT), first_playable=True)
        self.assertEqual({line["source_id"] for line in first}, {"s1"})

        cache_key = vod_sources._subject_cache_key(SUBJECT)
        deadline = time.monotonic() + 6
        cached = None
        while time.monotonic() < deadline:
            cached = registry._cache_get(cache_key)
            if cached and {line["source_id"] for line in cached} == {"s1", "s2"}:
                break
            time.sleep(0.05)
        self.assertIsNotNone(cached)
        self.assertEqual({line["source_id"] for line in cached}, {"s1", "s2"})

    def test_default_policy_still_waits_for_two_sources(self):
        """老调用方（wait_for_all / early_exit_source_count）口径不变。"""
        FakeAdapter.behaviors = {"s1": "fast", "s2": "fast", "s3": "slow"}
        registry = self._registry(["s1", "s2", "s3"])
        started = time.monotonic()
        lines = registry.resolve_subject(dict(SUBJECT))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual({line["source_id"] for line in lines}, {"s1", "s2"})

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


class DiskResolveCacheTest(RacingTestCase):
    """落盘解析缓存契约（2026-10-14）。

    App 冷启动时内嵌后端刚起来，内存短缓存必然为空；没有落盘缓存的话「第一次打开
    影片」只能吃完整网络延迟。这里锁住：终态结果落盘、新实例直接命中、过期不命中、
    文件损坏降级成没有缓存。
    """

    def _settled_registry(self, path, ids=("s1",)):
        registry = self._registry(list(ids))
        registry.enable_disk_cache(path)
        lines = registry.resolve_subject(dict(SUBJECT), first_playable=True)
        self.assertTrue(lines)
        registry.flush_disk_cache()
        registry.close()
        return lines

    def test_settled_result_is_reused_by_a_fresh_registry_without_any_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "vod_resolve_cache.json"
            first = self._settled_registry(path)
            self.assertTrue(path.exists())

            FakeAdapter.behaviors = {"s1": "dead"}
            FakeAdapter.calls = {}
            fresh = self._registry(["s1"])
            fresh.enable_disk_cache(path)
            self.addCleanup(fresh.close)

            second = fresh.resolve_subject(dict(SUBJECT))

            self.assertEqual([line["id"] for line in second], [line["id"] for line in first])
            self.assertEqual(FakeAdapter.calls, {}, "磁盘命中不该再触网搜索")

    def test_expired_entries_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "vod_resolve_cache.json"
            self._settled_registry(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            for entry in payload["entries"].values():
                entry["at"] = time.time() - vod_sources.DISK_CACHE_TTL_SECONDS - 60
            path.write_text(json.dumps(payload), encoding="utf-8")

            FakeAdapter.behaviors = {"s1": "fast"}
            FakeAdapter.calls = {}
            fresh = self._registry(["s1"])
            fresh.enable_disk_cache(path)
            self.addCleanup(fresh.close)
            lines = fresh.resolve_subject(dict(SUBJECT))

            self.assertTrue(lines)
            self.assertEqual(FakeAdapter.calls.get("s1"), 1, "过期条目必须回源")

    def test_corrupt_cache_file_degrades_to_no_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "vod_resolve_cache.json"
            path.write_text("{ not json", encoding="utf-8")
            registry = self._registry(["s1"])
            registry.enable_disk_cache(path)
            self.addCleanup(registry.close)
            self.assertTrue(registry.resolve_subject(dict(SUBJECT), first_playable=True))

    def test_invalidate_subject_drops_the_disk_entry_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "vod_resolve_cache.json"
            self._settled_registry(path)
            registry = self._registry(["s1"])
            registry.enable_disk_cache(path)
            self.addCleanup(registry.close)

            registry.invalidate_subject(dict(SUBJECT))
            registry.flush_disk_cache()
            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(payload["entries"], {})


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

    def test_private_addresses_are_rejected_before_any_socket_is_opened(self):
        """F-6 (split out): loopback is a security outcome, not a timeout outcome."""
        seen = []

        class RecordingSocket:
            def __init__(self, *args, **kwargs):
                pass

            def settimeout(self, _timeout):
                pass

            def connect(self, _address):
                seen.append("connect")

            def close(self):
                pass

        def loopback(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     "", ("127.0.0.1", port))]

        with patch("socket.getaddrinfo", side_effect=loopback), \
                patch("source_dns.socket.socket", side_effect=RecordingSocket):
            with self.assertRaises(OSError) as caught:
                source_dns._bounded_create_connection(("local.test", 80), timeout=1)
        self.assertIsInstance(caught.exception, OSError)
        self.assertEqual(seen, [], "no socket may be created for a private address")

    def test_timed_out_searches_self_release_bounded_pool_slots(self):
        """F-6: the per-source deadline must be what ends the job.

        The old version pointed the client at a loopback ``ThreadingHTTPServer``
        and only patched ``_validate_public_url``. The loopback address was
        rejected during the DNS safety check, so the assertions passed whether
        or not the deadline existed. Here DNS answers with a public address and
        ``socket.socket`` is stubbed, so the connection really is attempted and
        only the per-source deadline can end the search.
        """
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
        api_url = "http://public-source.test:%d/api.php/provide/vod/" % server.server_port

        connect_attempts = [0]
        connect_lock = threading.Lock()

        class SlowSocket:
            def __init__(self, *args, **kwargs):
                pass

            def settimeout(self, timeout):
                self._timeout = delay_seconds if timeout is None else float(timeout)

            def connect(self, address):
                with connect_lock:
                    connect_attempts[0] += 1
                # Model a connect that hangs for the server delay. A real socket
                # aborts it once its own timeout expires, so the connect can
                # only finish if the caller left a timeout longer than the
                # delay -- i.e. only if the per-source deadline did NOT apply.
                timeout = self._timeout
                end = time.monotonic() + delay_seconds
                while True:
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        return
                    if timeout is not None and remaining > timeout:
                        time.sleep(max(0.0, timeout))
                        raise socket.timeout("timed out")
                    time.sleep(min(0.005, remaining))

            def close(self):
                pass

        def public_dns(_host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     "", ("93.184.216.34", port))]

        registry = vod_sources.SourceRegistry(
            [vod_sources.VodSource(
                "s%d" % index, "s%d" % index, "http://public-source.test", api_url,
                "macms_json") for index in range(6)])
        try:
            with patch.object(vod_sources, "FAST_SOURCE_JOB_TIMEOUT_SECONDS", 0.08, create=True), \
                    patch("socket.getaddrinfo", side_effect=public_dns), \
                    patch("source_dns.socket.socket", side_effect=SlowSocket):
                started = time.monotonic()
                first = registry.submit_fast_searches(dict(SUBJECT))
                first_results = [future.result(timeout=2) for future in first]
                wall = time.monotonic() - started

                self.assertEqual(len(first), 3)
                self.assertTrue(all(result[2] is not None for result in first_results))
                self.assertLess(max(result[3] for result in first_results), 200)
                self.assertLess(wall, 0.3, "the deadline must end the job before the delay")
                self.assertGreaterEqual(
                    connect_attempts[0], 1,
                    "the request must reach connect(), otherwise this test again "
                    "only proves the private-address guard")

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

    def test_stalled_dns_does_not_hold_fast_source_workers(self):
        import socket
        import hhkan

        def stalled_getaddrinfo(_host, _port, *args, **kwargs):
            time.sleep(0.4)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     "", ("8.8.8.8", 443))]

        for adapter_type, site_url, api_url in (
                ("macms_json", "https://source.test", "https://source.test/api/"),
                ("hhkan", "https://www.hhkan0.com", "")):
            with self.subTest(adapter_type=adapter_type):
                registry = vod_sources.SourceRegistry([
                    vod_sources.VodSource("source", "source", site_url, api_url,
                                          adapter_type)])
                try:
                    with patch.object(vod_sources, "FAST_SOURCE_JOB_TIMEOUT_SECONDS", 0.08), \
                            patch("socket.getaddrinfo", side_effect=stalled_getaddrinfo), \
                            patch.object(hhkan, "_token_cache", None):
                        future = registry.submit_fast_searches(dict(SUBJECT))[0]
                        result = future.result(timeout=1)
                        self.assertIsNotNone(result[2])
                        self.assertLess(result[3], 250)

                        retry = ()
                        deadline = time.monotonic() + 0.5
                        while time.monotonic() < deadline and not retry:
                            retry = registry.submit_fast_searches(dict(SUBJECT))
                            if not retry:
                                time.sleep(0.01)
                        self.assertEqual(len(retry), 1)
                        retry_result = retry[0].result(timeout=1)
                        self.assertIsNotNone(retry_result[2])
                        self.assertLess(retry_result[3], 250)
                finally:
                    registry.close()


if __name__ == "__main__":
    unittest.main()

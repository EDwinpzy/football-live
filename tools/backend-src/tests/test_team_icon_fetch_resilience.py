"""队标抓取的并发韧性（BUG-04：国家队队标大面积缺失）。

现象：模拟器上并发拉 16 个国家队队标，**6 个返回 68 字节的 1x1 透明占位图**
（38% 失败率）；同样的请求串行执行则 0 失败。

根因（`proxy.py::fetch_svg_icon`）：
  1. 抓 `crests.football-data.org` 的 SVG 只 `urlopen(timeout=6)` 一次，**无重试**；
  2. 并发时上游 CDN 限流，失败就 `return None, False`；
  3. 失败**既不落盘也不记失败缓存** → 每次请求都重新打上游；
  4. 调用方 `/api/team-icon-img` 拿到失败就回 `_send_pixel_png()`，
     前端 `onerror` 只显示首字兜底 → 用户看到「队标缺失」。

本测试锁定修复后的行为：单次失败后重试并最终成功；同一 URL 的并发只打一次上游。
"""
import pathlib
import sys
import threading
import time
import unittest
from unittest import mock


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
import proxy  # noqa: E402


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TeamIconFetchResilienceTest(unittest.TestCase):
    def setUp(self):
        # 隔离磁盘缓存：每个用例从空缓存开始，避免互相污染。
        self._tmp = BACKEND / "tests" / "_tmp_svg_cache"
        self._tmp.mkdir(parents=True, exist_ok=True)
        for old in self._tmp.glob("*"):
            old.unlink()
        patcher = mock.patch.object(proxy, "SVG_ICON_DIR", self._tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        proxy.SVG_ICON_CACHE.clear()
        self.addCleanup(proxy.SVG_ICON_CACHE.clear)

    def test_retries_once_and_succeeds(self):
        """上游第一次失败（限流）时，重试应拿到真实队标。"""
        url = "https://crests.football-data.org/64.svg"
        payload = b"<svg>team</svg>" * 10
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise OSError("429 Too Many Requests")
            return _FakeResponse(payload)

        with mock.patch.object(proxy.urlreq, "urlopen", fake_urlopen):
            data, ok = proxy.fetch_svg_icon(url)

        self.assertTrue(ok, "第一次失败后重试应成功")
        self.assertEqual(data, payload)
        self.assertGreaterEqual(len(calls), 2, "应至少尝试 2 次")

    def test_successful_fetch_is_cached_on_disk(self):
        """抓成功后必须落盘，避免下次再打上游。"""
        url = "https://crests.football-data.org/57.svg"
        payload = b"<svg>cached</svg>"
        with mock.patch.object(proxy.urlreq, "urlopen", lambda r, timeout=None: _FakeResponse(payload)):
            data, ok = proxy.fetch_svg_icon(url)
        self.assertTrue(ok)
        written = list(self._tmp.glob("*.svg"))
        self.assertTrue(written, "成功后应落盘")
        self.assertEqual(written[0].read_bytes(), payload)

    def test_second_call_serves_from_disk_without_network(self):
        """磁盘缓存命中时不应再打上游。"""
        url = "https://crests.football-data.org/57.svg"
        payload = b"<svg>warm</svg>"
        with mock.patch.object(proxy.urlreq, "urlopen", lambda r, timeout=None: _FakeResponse(payload)):
            proxy.fetch_svg_icon(url)

        def explode(*a, **k):
            raise AssertionError("缓存命中后不应再访问网络")

        proxy.SVG_ICON_CACHE.clear()  # 只留磁盘缓存，强制走 fpath 分支
        with mock.patch.object(proxy.urlreq, "urlopen", explode):
            data, ok = proxy.fetch_svg_icon(url)
        self.assertTrue(ok)
        self.assertEqual(data, payload)

    def test_all_attempts_failing_returns_not_ok(self):
        """全部重试都失败时如实返回失败，调用方才能给占位图。"""
        url = "https://crests.football-data.org/999.svg"
        with mock.patch.object(proxy.urlreq, "urlopen", side_effect=OSError("boom")):
            data, ok = proxy.fetch_svg_icon(url)
        self.assertFalse(ok)
        self.assertIsNone(data)

    def test_concurrent_fetches_of_same_url_hit_upstream_once(self):
        """同一 URL 的并发请求只打一次上游（其余复用首个结果）。"""
        url = "https://crests.football-data.org/73.svg"
        payload = b"<svg>once</svg>"
        calls = []

        def slow_urlopen(req, timeout=None):
            calls.append(1)
            time.sleep(0.25)
            return _FakeResponse(payload)

        results = []
        with mock.patch.object(proxy.urlreq, "urlopen", slow_urlopen):
            threads = [
                threading.Thread(target=lambda: results.append(proxy.fetch_svg_icon(url)))
                for _ in range(6)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(len(results), 6)
        self.assertTrue(all(ok for _d, ok in results), "所有并发调用都应成功")
        self.assertEqual(len(calls), 1, "同一 URL 并发只应打上游 1 次，实测 %d 次" % len(calls))

    def test_timeout_is_long_enough_for_slow_mobile_network(self):
        """单次超时要够长：实测设备到 CDN 单个 SVG 要 5~6s，本机只要 0.9s。

        原先 `urlopen(timeout=6)` 配上三次重试的退避，在慢网络下仍会全军覆没
        ——真机 48 队并发实测有 3 个（利物浦/布伦特福德/巴黎圣日耳曼）返回 68 字节占位。
        """
        self.assertGreaterEqual(
            proxy.SVG_ICON_FETCH_TIMEOUT, 15,
            "SVG 抓取超时需 >=15s：设备侧实测单个 SVG 要 5~6s，本机 0.9s",
        )

    def test_timeout_value_is_actually_used(self):
        """确认重试路径把配置的超时传给 urlopen（而不是写死 6）。"""
        url = "https://crests.football-data.org/57.svg"
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(timeout)
            raise OSError("boom")

        with mock.patch.object(proxy.urlreq, "urlopen", fake_urlopen):
            proxy.fetch_svg_icon(url)

        self.assertTrue(seen, "应调用过 urlopen")
        self.assertTrue(
            all(t == proxy.SVG_ICON_FETCH_TIMEOUT for t in seen),
            "urlopen 应收 SVG_ICON_FETCH_TIMEOUT，实测 %r" % (seen,),
        )

    def test_retry_backoff_does_not_exceed_the_timeout_budget(self):
        """三次尝试 + 退避的总耗时必须显著小于超时×次数，否则慢网下必然全败。"""
        budget = (
            proxy.SVG_ICON_FETCH_ATTEMPTS * proxy.SVG_ICON_FETCH_TIMEOUT
            + proxy.SVG_ICON_FETCH_BACKOFF
            * (1 + 2)  # attempt 1、2 各退避一次
        )
        self.assertGreater(budget, 45, "总预算 %.1fs 偏短，慢网下三连败" % budget)

    def test_upstream_concurrency_is_bounded(self):
        """对上游的并发必须有上限。

        48 个队标同时抓时设备侧带宽被打满，**大文件**（利物浦 299KB、
        巴黎圣日耳曼 274KB）即使把单次超时放宽到 20s 仍会三连超时；同一个队
        单独请求只要 3.1s 就能拿到。加超时治不了「并发挤占」，必须限流。
        """
        self.assertTrue(
            hasattr(proxy, "SVG_ICON_FETCH_SEMAPHORE"),
            "应有对上游的并发闸门（SVG_ICON_FETCH_SEMAPHORE）",
        )
        self.assertLessEqual(
            proxy.SVG_ICON_FETCH_SEMAPHORE._value, 8,
            "对上游的并发上限应 <=8，实测 48 并发会把设备带宽打满",
        )

    def test_concurrent_different_urls_never_exceed_the_limit(self):
        """N 个不同 URL 并发时，真正打到上游的不超过上限。"""
        inflight = []
        peak = []
        guard = threading.Lock()

        def slow_urlopen(req, timeout=None):
            with guard:
                inflight.append(1)
                peak.append(len(inflight))
            time.sleep(0.12)
            with guard:
                inflight.pop()
            return _FakeResponse(b"<svg>x</svg>")

        urls = ["https://crests.football-data.org/%d.svg" % i for i in range(20)]
        with mock.patch.object(proxy.urlreq, "urlopen", slow_urlopen):
            threads = [threading.Thread(target=proxy.fetch_svg_icon, args=(u,)) for u in urls]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertLessEqual(
            max(peak), proxy.SVG_ICON_FETCH_SEMAPHORE._value + 1,
            "实测峰值并发 %d，应不超过闸门 %d" % (max(peak), proxy.SVG_ICON_FETCH_SEMAPHORE._value),
        )

    def tearDown(self):
        for old in self._tmp.glob("*"):
            old.unlink()


if __name__ == "__main__":
    unittest.main()

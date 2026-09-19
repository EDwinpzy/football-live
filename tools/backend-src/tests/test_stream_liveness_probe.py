"""v1.32 交付前活性探测（_probe_alive + resolve_stream 全源探测）契约测试。

背景：2026-09-19 奥克兰FC 友谊赛 plu 线 lmpull.va02.com 为「挂起型死地址」
（TCP 通、根路径秒 404、/sport/* 挂起 ~16s 断）。旧逻辑只对 qqlive 源探测，
bb/plu 死链直达播放器，用户白等 ~20s 起播看门狗。现全源探测，死链按
「未开播」语义返回（ts=0 不缓存，前端 30s 重试时重新解析，来流后自愈）。
"""
import pathlib
import sys
import unittest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class _FakeResp:
    """urllib3 / urlreq 两分支同型的假响应（.status / .read / 释放方法）。"""

    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def read(self, *_args, **_kwargs):
        return self._body

    def release_conn(self):
        pass

    def close(self):
        pass


class ProbeAliveTest(unittest.TestCase):
    """_probe_alive 单元行为：双 Referer 策略、短超时、首字节判定。"""

    def setUp(self):
        self.old = (proxy._open_upstream, proxy._relay_strategies)
        proxy._relay_strategies = lambda _ref: [{"Referer": "yes"}, {"Referer": "none"}]

    def tearDown(self):
        proxy._open_upstream, proxy._relay_strategies = self.old

    def test_alive_on_first_strategy_200_with_bytes(self):
        calls = []
        proxy._open_upstream = lambda u, h, **kw: calls.append(h) or (_FakeResp(200, b"#EXTM3U"), u)
        self.assertTrue(proxy._probe_alive("http://cdn.example/a.m3u8"))
        self.assertEqual(len(calls), 1)  # 首策略即活，不烧第二策略

    def test_dead_when_all_strategies_timeout(self):
        calls = []

        def hang(u, h, **kw):
            calls.append(h)
            raise TimeoutError("read timed out")

        proxy._open_upstream = hang
        self.assertFalse(proxy._probe_alive("http://cdn.example/dead.m3u8"))
        self.assertEqual(len(calls), 2)  # 两策略都试才判死

    def test_second_strategy_rescues_referer_rejecting_cdn(self):
        # njscwh 型：带 Referer 403、无 Referer 秒开（v1.22 注释记载的真实形态）
        calls = []

        def by_strategy(u, h, **kw):
            calls.append(h)
            if h.get("Referer") == "yes":
                return _FakeResp(403), u
            return _FakeResp(200, b"#EXTM3U"), u

        proxy._open_upstream = by_strategy
        self.assertTrue(proxy._probe_alive("http://cdn.example/a.m3u8"))
        self.assertEqual(len(calls), 2)

    def test_empty_body_is_dead(self):
        proxy._open_upstream = lambda u, h, **kw: (_FakeResp(200, b""), u)
        self.assertFalse(proxy._probe_alive("http://cdn.example/empty.m3u8"))

    def test_server_error_is_dead(self):
        proxy._open_upstream = lambda u, h, **kw: (_FakeResp(500, b"x"), u)
        self.assertFalse(proxy._probe_alive("http://cdn.example/err.m3u8"))

    def test_non_http_url_rejected_without_network(self):
        calls = []
        proxy._open_upstream = lambda u, h, **kw: calls.append(u)
        self.assertFalse(proxy._probe_alive("error:xxx"))
        self.assertEqual(calls, [])


class ResolveStreamProbeContractTest(unittest.TestCase):
    """resolve_stream 全源探测契约：死链 → 未开播语义且不缓存；活链 → 透传并缓存。"""

    def setUp(self):
        self.old = (proxy.fetch_url, proxy.decrypt_page, proxy._stream_via_cloud,
                    proxy._probe_alive)
        proxy.STREAM_CACHE.clear()
        proxy._stream_via_cloud = lambda *_a, **_k: None
        proxy.fetch_url = lambda url, **_kw: (
            '<iframe src="http://player/ballbar.php?id=1"></iframe>' if "/tv/" in url else "player"
        )
        proxy.decrypt_page = lambda *_a, **_k: {"url": "https://cdn.example/live.m3u8"}

    def tearDown(self):
        (proxy.fetch_url, proxy.decrypt_page, proxy._stream_via_cloud,
         proxy._probe_alive) = self.old
        proxy.STREAM_CACHE.clear()

    def test_dead_url_falls_back_to_not_started_semantics_and_stays_uncached(self):
        proxy._probe_alive = lambda *_a, **_k: False
        payload, _ = proxy.resolve_stream("200", "plu")
        self.assertIsNone(payload["url"])
        self.assertEqual(payload["error"], "信号尚未开播（开赛后自动可用）")
        # ts=0 不缓存：下一次请求必须重新解析（缓存里不能有这条死结果）
        payload2, cached2 = proxy.resolve_stream("200", "plu")
        self.assertFalse(cached2)
        self.assertIsNone(payload2["url"])

    def test_alive_url_passes_through_and_caches(self):
        proxy._probe_alive = lambda *_a, **_k: True
        payload, _ = proxy.resolve_stream("201", "bb")
        self.assertEqual(payload["url"], "https://cdn.example/live.m3u8")
        _, cached = proxy.resolve_stream("201", "bb")
        self.assertTrue(cached)

    def test_probe_exception_is_fail_open(self):
        def boom(*_a, **_k):
            raise RuntimeError("probe bug")

        proxy._probe_alive = boom
        payload, _ = proxy.resolve_stream("202", "bb")
        self.assertEqual(payload["url"], "https://cdn.example/live.m3u8")

    def test_qqlive_src_also_probed(self):
        # qqlive 原有探测改走 _probe_alive（旧 relay_url 探测 12s×2 太慢）
        proxy._probe_alive = lambda *_a, **_k: False
        payload, _ = proxy.resolve_stream("203", "qqlive100")
        self.assertIsNone(payload["url"])
        self.assertEqual(payload["error"], "信号尚未开播（开赛后自动可用）")


if __name__ == "__main__":
    unittest.main()

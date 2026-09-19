import pathlib
import sys
import unittest


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def time(self):
        return self.now


class StreamCacheRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.old = (proxy.fetch_url, proxy.decrypt_page, proxy._stream_via_cloud,
                    proxy._probe_alive, proxy.time)
        proxy.STREAM_CACHE.clear()
        proxy._stream_via_cloud = lambda *_args, **_kwargs: None
        # v1.32 起 resolve_stream 对成功解析做交付前活性探测；缓存语义测试与
        # 网络无关，一律按存活处理（探测行为本身由 test_stream_liveness_probe 覆盖）。
        proxy._probe_alive = lambda *_args, **_kwargs: True
        self.clock = Clock()
        proxy.time = self.clock

    def tearDown(self):
        (proxy.fetch_url, proxy.decrypt_page, proxy._stream_via_cloud,
         proxy._probe_alive, proxy.time) = self.old
        proxy.STREAM_CACHE.clear()

    def test_missing_player_is_retried_on_next_request(self):
        calls = []
        proxy.fetch_url = lambda *_args, **_kwargs: calls.append(1) or "<html>no iframe</html>"
        first, first_cached = proxy.resolve_stream("100", "bb")
        second, second_cached = proxy.resolve_stream("100", "bb")
        self.assertFalse(first_cached)
        self.assertFalse(second_cached)
        self.assertEqual(first["error"], "未找到播放器地址")
        self.assertEqual(len(calls), 2)

    def test_exception_is_retried_on_next_request(self):
        calls = []

        def fail(*_args, **_kwargs):
            calls.append(1)
            raise OSError("temporary")

        proxy.fetch_url = fail
        proxy.resolve_stream("101", "bb")
        _payload, cached = proxy.resolve_stream("101", "bb")
        self.assertFalse(cached)
        self.assertEqual(len(calls), 2)

    def test_success_cache_expires_after_sixty_seconds(self):
        calls = []

        def fetch(url, **_kwargs):
            calls.append(url)
            return '<iframe src="http://player/ballbar.php?id=1"></iframe>' if "/tv/" in url else "player"

        proxy.fetch_url = fetch
        proxy.decrypt_page = lambda *_args, **_kwargs: {"url": "https://cdn.example/live.m3u8"}
        proxy.resolve_stream("102", "bb")
        self.clock.now += 59
        _payload, cached = proxy.resolve_stream("102", "bb")
        self.assertTrue(cached)
        self.clock.now += 2
        _payload, cached = proxy.resolve_stream("102", "bb")
        self.assertFalse(cached)
        self.assertEqual(len(calls), 4)


if __name__ == "__main__":
    unittest.main()


"""足球信号源请求不得与其它接口共用 OkHttp 并发名额（2026-10-17 回归契约）。

现场：赛程与信号源请求打到 127.0.0.1:8090，OkHttp 默认
maxRequestsPerHost=5。旧版 liveSourceHttp 由主客户端 newBuilder 派生，沿用同一个
Dispatcher，进足球页瞬间被慢接口占满 → 信号源请求排队 → callTimeout 到点抛
InterruptedIOException → 播放器「无可用信号源」，只有杀进程才恢复。

这里锁住修复形态：两端 liveSourceHttp 必须自带 dispatcher(...) 配置，
后端必须保留 src=all 的短缓存与失败回退常量。
"""
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
LIVE_REPOSITORIES = (
    "android/app/src/main/java/com/qiubo/optimaltv/data/repo/LiveRepository.kt",
    "internal/mobile/app/src/main/java/com/qiubo/optimaltv/data/repo/LiveRepository.kt",
)


class LiveSourceDispatcherContractTest(unittest.TestCase):
    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_live_source_client_owns_a_dedicated_dispatcher(self):
        for relative in LIVE_REPOSITORIES:
            with self.subTest(repository=relative):
                source = self.read(relative)
                block = source[source.index("private val liveSourceHttp"):]
                block = block[:block.index("private suspend fun getLiveSource")]
                self.assertIn(".dispatcher(", block)
                self.assertIn("maxRequestsPerHost", block)
                # dispatcher 必须在 build() 之前挂上，否则仍是共用主客户端的队列
                self.assertLess(block.index(".dispatcher("), block.index(".build()"))

    def test_source_request_logs_elapsed_time_for_field_diagnosis(self):
        for relative in LIVE_REPOSITORIES:
            with self.subTest(repository=relative):
                source = self.read(relative)
                method = source[source.index("suspend fun liveStreamSources"):]
                method = method[:method.index("suspend fun refreshLiveStreamSource")]
                self.assertIn("System.currentTimeMillis()", method)
                self.assertIn("线路解析失败", method)

    def test_backend_keeps_the_all_source_cache_and_stale_fallback(self):
        source = self.read("tools/backend-src/proxy.py")
        for marker in (
            "STREAM_ALL_TTL",
            "STREAM_ALL_STALE_TTL",
            "STREAM_ALL_FRESH_COALESCE",
            "def _resolve_88zhibo_streams_uncached",
            "def resolve_88zhibo_streams",
            '"stale"] = True',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()

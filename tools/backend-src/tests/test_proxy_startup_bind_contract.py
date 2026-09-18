"""2026-09-18 回归护栏：端口绑定失败时不得启动预热。

背景：TV 版/移动版同装一台设备时 8090 只有一方能绑定，绑定失败方由 proxy_runner
每 5s 重试接管。旧实现把 `_startup_warm` 放在 `ThreadingHTTPServer(...)` 之前——
每次重试都重建一遍影视索引（MediaIndex 建库 + 22MB 快照预热），失败方会在 20 分钟里
每 5 秒空转一次，实测模拟器上持续占 CPU，并拖慢同机另一侧 app 的请求。
"""
import pathlib
import sys
import threading
import unittest
from unittest import mock

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class _ThreadStub:
    """同步执行 target 的 Thread 替身：把「谁先启动」变成可断言的调用序列。"""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()

    def join(self, timeout=None):
        pass


class _ThreadingStub:
    Thread = _ThreadStub

    def __getattr__(self, name):
        return getattr(threading, name)


def _server_stub(events, fail):
    class _Server:
        def __init__(self, addr, handler):
            events.append("bind")
            if fail:
                raise OSError(98, "Address already in use")

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            pass

    return _Server


class StartupBindOrder(unittest.TestCase):
    def _run_main(self, fail):
        events = []
        with mock.patch.object(proxy, "ThreadingHTTPServer", _server_stub(events, fail)), \
                mock.patch.object(proxy, "_startup_warm", lambda: events.append("warm")), \
                mock.patch.object(proxy, "_img_domain_refresher", lambda: events.append("img")), \
                mock.patch.object(proxy, "threading", _ThreadingStub()):
            try:
                proxy.main(port=0, bind="127.0.0.1")
            except OSError:
                pass
        return events

    def test_port_conflict_does_not_warm(self):
        """绑定失败（同机另一侧占着 8090）：不得预热，否则每 5s 重试都白烧一次索引构建。"""
        self.assertEqual(self._run_main(fail=True), ["bind"])

    def test_bind_precedes_warm(self):
        """绑定成功：先绑端口再预热（容器 ready 探针也因此更早可用）。"""
        self.assertEqual(self._run_main(fail=False), ["bind", "warm", "img"])


if __name__ == "__main__":
    unittest.main()

"""源匹配韧性 + 代理 fake-IP 放行契约。

两个线上事故点：
1) 9 个源同时失联（弱网/代理抖动）时旧实现直接回空列表，用户看到「没有信号源」——
   实际片源是好的。这里锁住「有异常才重试一次、正常应答不重试」。
2) 本机 DNS 走 Clash/Surge 的 fake-IP（域名统一解析到 198.18.0.0/15）时，
   旧校验把 8 个域名源全部判成「内网」拦掉，只剩直连 IP 的「好好看」。
   锁住「整组都是 fake-IP 放行；混入真实内网照旧拦截」。
"""
import pathlib
import socket
import sys
import unittest


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import vod_sources  # noqa: E402


def _resolve_to(*addresses):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 443))
                for address in addresses]

    return fake_getaddrinfo


class ProxyFakeIpTest(unittest.TestCase):
    def setUp(self):
        self._real = socket.getaddrinfo
        self._delay = vod_sources.RESOLVE_RETRY_DELAY
        vod_sources.RESOLVE_RETRY_DELAY = 0

    def tearDown(self):
        socket.getaddrinfo = self._real
        vod_sources.RESOLVE_RETRY_DELAY = self._delay

    def test_domain_on_fake_ip_pool_is_allowed(self):
        socket.getaddrinfo = _resolve_to("198.18.0.30")
        url = "https://bfzyapi.com/api.php/provide/vod/"
        self.assertEqual(vod_sources._validate_public_url(url, resolve_dns=True), url)

    def test_domain_on_private_ip_is_still_blocked(self):
        socket.getaddrinfo = _resolve_to("192.168.1.10")
        with self.assertRaises(vod_sources.UnsafeSourceUrl):
            vod_sources._validate_public_url("https://evil.test/api", resolve_dns=True)

    def test_mixed_fake_ip_and_private_is_blocked(self):
        socket.getaddrinfo = _resolve_to("198.18.0.30", "10.0.0.5")
        with self.assertRaises(vod_sources.UnsafeSourceUrl):
            vod_sources._validate_public_url("https://evil.test/api", resolve_dns=True)

    def test_literal_fake_ip_address_is_blocked(self):
        with self.assertRaises(vod_sources.UnsafeSourceUrl):
            vod_sources._validate_public_url("http://198.18.0.30/api", resolve_dns=True)

    def test_loopback_hostname_still_blocked(self):
        socket.getaddrinfo = _resolve_to("198.18.0.30")
        with self.assertRaises(vod_sources.UnsafeSourceUrl):
            vod_sources._validate_public_url("http://localhost/api", resolve_dns=True)


class ProbeBudgetTest(unittest.TestCase):
    """探测预算：9 个源全通时线路 40+ 条，逐条探测实测 15~40 秒。锁住「按源轮转取样」。"""

    def _line(self, source, index):
        return {"id": "%s:%d" % (source, index), "source_id": source, "alive": None}

    def test_round_robin_keeps_every_source_in_budget(self):
        lines = [self._line("hhkan", i) for i in range(33)]
        lines += [self._line("bfzy", 0), self._line("jszy", 0), self._line("jinying", 0)]
        picked = vod_sources._pick_probe_lines(lines, 6)
        self.assertEqual(len(picked), 6)
        self.assertEqual({line["source_id"] for line in picked},
                         {"hhkan", "bfzy", "jszy", "jinying"})
        # 线路最多的源不能把预算占满
        self.assertLessEqual(sum(1 for line in picked if line["source_id"] == "hhkan"), 3)

    def test_under_budget_returns_everything(self):
        lines = [self._line("hhkan", i) for i in range(5)]
        self.assertEqual(len(vod_sources._pick_probe_lines(lines, 18)), 5)

    def test_probe_budget_constants_are_bounded(self):
        self.assertLessEqual(vod_sources.PROBE_MAX_LINES, 24)
        self.assertLessEqual(vod_sources.PROBE_TIMEOUT_SECONDS, 5)


class ResolveRetryTest(unittest.TestCase):
    def setUp(self):
        self._real_hhkan = vod_sources.HhkanAdapter
        self._delay = vod_sources.RESOLVE_RETRY_DELAY
        vod_sources.RESOLVE_RETRY_DELAY = 0
        self.addCleanup(self._restore)

    def _restore(self):
        vod_sources.HhkanAdapter = self._real_hhkan
        vod_sources.RESOLVE_RETRY_DELAY = self._delay

    def _registry(self, count=3):
        return vod_sources.SourceRegistry([
            vod_sources.VodSource(id="s%d" % i, name="源%d" % i, site_url="https://example.test",
                                  api_url="https://example.test/api.php/provide/vod/",
                                  adapter_type="hhkan", enabled=True, revision=1)
            for i in range(count)
        ], version=1)

    def test_all_sources_failing_is_retried_once_and_can_recover(self):
        state = {"round": 0}

        class RecoveringAdapter:
            def __init__(self, source):
                self.source = source

            def search(self, subject):
                state["round"] += 1
                if state["round"] <= 3:        # 第一轮三台源全部失联
                    raise RuntimeError("network jitter")
                return [{"title": "肖申克的救赎", "year": "1994", "category": "movie", "season": 0}]

            def detail(self, candidate):
                return [{"id": self.source.id + ":1", "source_id": self.source.id, "name": "线",
                         "height": 1080, "latency_ms": 300, "alive": True}]

        vod_sources.HhkanAdapter = RecoveringAdapter
        subject = {"title": "肖申克的救赎", "year": "1994", "category": "movie", "season": 0}
        lines = self._registry().resolve_subject(subject)
        self.assertEqual(len(lines), 3)
        self.assertGreater(state["round"], 3)          # 确实发生了第二轮

    def test_no_match_without_failures_is_not_retried(self):
        calls = {"n": 0}

        class QuietAdapter:
            def __init__(self, source):
                self.source = source

            def search(self, subject):
                calls["n"] += 1
                return []                              # 应答正常，只是没有这部片

            def detail(self, candidate):
                return []

        vod_sources.HhkanAdapter = QuietAdapter
        subject = {"title": "不存在的片子", "year": "1994", "category": "movie", "season": 0}
        self.assertEqual(self._registry().resolve_subject(subject), [])
        self.assertEqual(calls["n"], 3)                # 3 个源各查一次，没有二次等待


if __name__ == "__main__":
    unittest.main()

"""影视源的**设备侧**可用性探测（补 VPS 测速的盲区）。

问题：后台「测速」跑在 VPS 上，而 App 的内嵌后端跑在**用户手机**上，两者是
完全不同的网络出口。磐石云没有境外出口，挂在 Cloudflare 段的源从 VPS 必然
超时——2026-09-30 实测 10 个源里有 8 个在后台显示「失败」，但同一批源在
**实机网络下 7 个能正常取流**（取流 200 OK）。后台的「失败」是出口问题，
不是源失效；照着它清理会砍掉 App 里能用的线路。

因此需要一个走设备网络的探测通道：
  - App 侧调用它 → 结果反映**用户真实网络**下各源能不能用
  - 后台侧调用它 → 看 App 端报回来的健康度，而不是 VPS 自己 ping

设计要点：
  1. 探测走内嵌后端已有的 HTTP 栈（urllib），不引入新依赖；
  2. 并发受限（源多时串行会拖到几十秒），且**每个源都有独立超时**，
     一个源卡住不能拖垮整批；
  3. 结果只回「能不能用 + 延时 + 错误摘要」，**不回显 Cookie/Token**
     （源站 URL 本身是公开的，但错误信息可能带凭据，必须截断净化）。
"""
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
VOD_SOURCES = ROOT / "tools" / "backend-src" / "vod_sources.py"
ADMIN_SERVER = ROOT / "tools" / "admin_server.py"
ADMIN_HTML = ROOT / "tools" / "admin.html"


class VodSourceDeviceProbeContractTest(unittest.TestCase):
    def test_vod_sources_exposes_a_probe_helper(self):
        src = VOD_SOURCES.read_text(encoding="utf-8")
        self.assertIn("def probe_source(", src)
        self.assertIn("def probe_all_sources(", src)

    def test_probe_is_bounded_in_concurrency_and_timeout(self):
        """并发与单源超时都要有上限，否则一整批能卡到几分钟。"""
        src = VOD_SOURCES.read_text(encoding="utf-8")
        self.assertIn("PROBE_TIMEOUT", src)
        self.assertIn("PROBE_CONCURRENCY", src)
        # 超时必须真的传进 urlopen，而不是摆设的常量
        self.assertIn("timeout=PROBE_TIMEOUT", src)

    def test_probe_does_not_leak_credentials(self):
        """错误摘要必须截断——上游报错可能把 URL 里的 token 带出来。"""
        src = VOD_SOURCES.read_text(encoding="utf-8")
        self.assertIn("[:120]", src)

    def test_probe_uses_macs_list_endpoint_for_reachability(self):
        """探测走 `?ac=list`（最轻的接口），不该为探活就拉整页数据。"""
        src = VOD_SOURCES.read_text(encoding="utf-8")
        self.assertIn("ac=list", src)

    def test_admin_exposes_device_probe_endpoint(self):
        server = ADMIN_SERVER.read_text(encoding="utf-8")
        self.assertIn("def h_vod_source_probe_device", server)
        self.assertIn('"/api/vod_source_probe_device"', server)

    def test_admin_endpoint_does_not_probe_from_the_vps(self):
        """后台不自己 ping 源，而是转发到 App 报上来的内嵌后端地址。"""
        server = ADMIN_SERVER.read_text(encoding="utf-8")
        body = server.split("def h_vod_source_probe_device", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("vod_source_admin_speed", body)

    def test_frontend_has_a_device_probe_entry(self):
        html = ADMIN_HTML.read_text(encoding="utf-8")
        self.assertIn("vod_source_probe_device", html)


if __name__ == "__main__":
    unittest.main()

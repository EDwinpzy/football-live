"""激活请求的网关异常契约。

2026-09-16 用户报障：投影仪上激活显示「激活失败502」。查下来底层把「响应不是 JSON」
一律记成 502，把真实 HTTP 状态吞了——403 拦截页会被误读成「该卡密已被封禁」。
这里锁住：保留真实状态码、带正常 UA、5xx/非 JSON 原地重试一次。
"""
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
TV = ROOT / "android/app/src/main/java/com/qiubo/optimaltv/license/LicenseManager.kt"
MOBILE = ROOT / "internal/mobile/app/src/main/java/com/qiubo/optimaltv/license/LicenseManager.kt"


class LicenseGatewayErrorContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = TV.read_text(encoding="utf-8")
        cls.mobile = MOBILE.read_text(encoding="utf-8")

    def test_non_json_keeps_real_http_status(self):
        start = self.src.index("fun once(")
        block = self.src[start:self.src.index("val attempts = listOf(", start)]
        self.assertIn("response.code.takeIf { it >= 400 } ?: 502", block)
        self.assertIn("bad_json:${response.code}", block)
        # 旧实现固定回 502，会把 403 拦截页伪装成网关错误
        self.assertNotIn('EndpointResp(502, null, "bad_json"', self.src)

    def test_activation_maps_gateway_error_before_business_codes(self):
        start = self.src.index("suspend fun activate(")
        block = self.src[start:self.src.index("fun requestReverify()", start)]
        gateway = block.index('resp.msg?.startsWith("bad_json")')
        business = block.index("return when (resp.ret)")
        self.assertLess(gateway, business, "网关异常必须先判，否则 403 会命中「已被封禁」")
        self.assertIn("服务器无响应（HTTP ${resp.ret}）", block)

    def test_transient_gateway_failure_retries_with_next_tier(self):
        """网关异常换档重试；业务码（卡密不存在、设备数满）是真实结论，不重试。

        2026-09-17：`70dcd7e` 把「原地重试一次」升级成三档（浏览器 UA → Chrome UA →
        Chrome UA + HTTP/1.1），原文断言 `val first = once()` 已不存在——这里按现行实现锁契约。
        """
        start = self.src.index("val attempts = listOf(")
        block = self.src[start:self.src.index("private fun parseTimestamp", start)]
        self.assertIn('resp.ret >= 500 || resp.msg?.startsWith("bad_json") == true', block)
        self.assertIn("if (!retryable) return@withContext resp", block)
        self.assertIn("for ((client, ua, label) in attempts)", block)
        # 至少两档重试（首个 UA 失败后还能换一个更大众脸的 UA），单档实现不算满足契约
        self.assertGreaterEqual(block.count("Triple(http"), 2)

    def test_request_carries_explicit_user_agent(self):
        """`70dcd7e`：自有接口统一用数据层同款浏览器 UA。

        实测投影仪发 `MyTV/x.y (Android)` 会被 Cloudflare 拦成 403 拦截页，请求到不了服务端；
        所以这里锁「请求头显式带 UA」+「UA 取自共享 HttpUa」，别再退回 OkHttp 默认 UA。
        """
        start = self.src.index("fun once(")
        block = self.src[start:self.src.index("val attempts = listOf(", start)]
        self.assertIn('.header("User-Agent", ua)', block)
        self.assertIn("UA_BROWSER = com.qiubo.optimaltv.data.net.HttpUa.BROWSER", self.src)
        # 兜底档换一个更「大众脸」的 Chrome UA
        self.assertIn("UA_CHROME", self.src)
        self.assertNotIn('.header("User-Agent", "MyTV/', self.src)

    def test_both_app_trees_identical(self):
        self.assertEqual(self.src, self.mobile, "两工程不一致，请跑 tools/sync_shared.py")


if __name__ == "__main__":
    unittest.main()

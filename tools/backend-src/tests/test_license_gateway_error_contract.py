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
        start = self.src.index("fun once(): EndpointResp?")
        block = self.src[start:self.src.index("val first = once()", start)]
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

    def test_transient_gateway_failure_retries_once(self):
        start = self.src.index("val first = once()")
        block = self.src[start:self.src.index("private fun parseTimestamp", start)]
        self.assertIn("first.ret >= 500", block)
        self.assertIn('first.msg?.startsWith("bad_json") == true', block)
        self.assertIn("once() ?: first", block)

    def test_request_carries_explicit_user_agent(self):
        self.assertIn('.header("User-Agent", "MyTV/${BuildConfig.VERSION_NAME} (Android)")', self.src)

    def test_both_app_trees_identical(self):
        self.assertEqual(self.src, self.mobile, "两工程不一致，请跑 tools/sync_shared.py")


if __name__ == "__main__":
    unittest.main()

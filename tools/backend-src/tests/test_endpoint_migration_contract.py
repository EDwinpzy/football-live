import unittest
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[3]
APP_CONFIG = ROOT / "android" / "app"
MOBILE_CONFIG = ROOT / "internal" / "mobile" / "app"


class EndpointMigrationContractTests(unittest.TestCase):
    def test_both_apks_accept_one_https_origin_for_backend_endpoints(self):
        for module in (APP_CONFIG, MOBILE_CONFIG):
            with self.subTest(module=module):
                gradle = (module / "build.gradle").read_text(encoding="utf-8")
                self.assertIn("apiBaseUrl", gradle)
                self.assertIn("LICENSE_ENDPOINT", gradle)
                self.assertIn("HOTUPDATE_ENDPOINT", gradle)
                self.assertIn("IPTV_LATEST_URL", gradle)
                self.assertIn("APK_UPDATE_URL", gradle)

    def test_release_defaults_target_the_vps_and_next_forced_migration_version(self):
        for module in (APP_CONFIG, MOBILE_CONFIG):
            with self.subTest(module=module):
                gradle = (module / "build.gradle").read_text(encoding="utf-8")
                # 正式包不再有 IP 默认入口：必须显式传入已验收的正式域名。
                # 完整校验规则见 tools/tests/test_release_api_origin_contract.py。
                self.assertNotIn("def defaultApiBaseUrl", gradle)
                self.assertIn("otvMigrationTestOrigin = 'https://103.236.97.137:35208'", gradle)
                self.assertIn("OTV_RELEASE_API_BASE_URL", gradle)
                self.assertIn("Integer.parseInt(apkVersionCodeOverride.toString()) : 110", gradle)
                self.assertIn("apkVersionNameOverride.toString() : \"1.1\"", gradle)

    def test_network_security_keeps_local_http_backend_working(self):
        for module in (APP_CONFIG, MOBILE_CONFIG):
            with self.subTest(module=module):
                manifest = (module / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
                config = ElementTree.parse(
                    module / "src" / "main" / "res" / "xml" / "network_security_config.xml"
                ).getroot()
                self.assertIn('android:networkSecurityConfig="@xml/network_security_config"', manifest)
                base = config.find("base-config")
                self.assertIsNotNone(base)
                self.assertEqual(base.attrib.get("cleartextTrafficPermitted"), "true")
                # 正式包只信任系统 CA：迁移 CA 与 IP 测试入口只留在 debug source set。
                self.assertEqual(config.findall("domain-config"), [])
                debug_config = module / "src" / "debug" / "res" / "xml" / "network_security_config.xml"
                self.assertTrue(debug_config.is_file(), "Debug 必须保留迁移 CA 覆盖配置")
                debug_root = ElementTree.parse(debug_config).getroot()
                host_config = next(
                    block for block in debug_root.findall("domain-config")
                    if block.findtext("domain") == "103.236.97.137"
                )
                self.assertEqual(host_config.attrib.get("cleartextTrafficPermitted"), "false")
                self.assertIn(
                    "@raw/mytv_migration_ca",
                    [item.attrib.get("src") for item in host_config.findall("trust-anchors/certificates")],
                )

    def test_both_iptv_latest_sources_use_the_configured_backend_origin(self):
        for module in (APP_CONFIG, MOBILE_CONFIG):
            with self.subTest(module=module):
                repository = (
                    module / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv"
                    / "data" / "repo" / "IptvRepository.kt"
                ).read_text(encoding="utf-8")
                self.assertIn("BuildConfig.IPTV_LATEST_URL", repository)
                self.assertNotIn("tcloudbase.com", repository)


if __name__ == "__main__":
    unittest.main()

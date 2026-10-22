import re
import unittest
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[3]
APP_CONFIG = ROOT / "android" / "app"
MOBILE_CONFIG = ROOT / "internal" / "mobile" / "app"


class EndpointMigrationContractTests(unittest.TestCase):
    def test_both_apks_keep_only_football_and_update_endpoints(self):
        for module in (APP_CONFIG, MOBILE_CONFIG):
            with self.subTest(module=module):
                gradle = (module / "build.gradle").read_text(encoding="utf-8")
                self.assertIn("apiBaseUrl", gradle)
                self.assertIn("HOTUPDATE_ENDPOINT", gradle)
                self.assertIn("APK_UPDATE_URL", gradle)
                self.assertNotIn("LICENSE_ENDPOINT", gradle)
                self.assertNotIn("IPTV_LATEST_URL", gradle)

    def test_release_defaults_require_the_vps_opt_in_and_do_not_regress_migration_version(self):
        for module in (APP_CONFIG, MOBILE_CONFIG):
            with self.subTest(module=module):
                gradle = (module / "build.gradle").read_text(encoding="utf-8")
                # 正式包不再有 IP 默认入口：必须显式传入已验收的正式域名。
                # 完整校验规则见 tools/tests/test_release_api_origin_contract.py。
                self.assertNotIn("def defaultApiBaseUrl", gradle)
                self.assertIn("otvMigrationTestOrigin = 'https://103.236.97.137:35208'", gradle)
                self.assertIn("OTV_RELEASE_API_BASE_URL", gradle)
                # 后续正式发布必须可以递增，不能被迁移当时的 V1.1/110 锁住。
                code = re.search(r"Integer\.parseInt\(apkVersionCodeOverride\.toString\(\)\)\s*:\s*(\d+)", gradle)
                name = re.search(r'apkVersionNameOverride\.toString\(\)\s*:\s*"(\d+\.\d+(?:\.\d+)?)"', gradle)
                self.assertIsNotNone(code)
                self.assertIsNotNone(name)
                self.assertGreaterEqual(int(code.group(1)), 110)
                self.assertGreaterEqual(tuple(map(int, name.group(1).split("."))), (1, 1))

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
                # main 兜底只信任系统 CA；迁移 CA 的 IP domain-config 只在 debug
                # 与显式 opt-in 挂载的 ipRelease 覆盖（-PallowIpReleaseOrigin=true）里出现。
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
                # 无正式域名时的发布通道：opt-in 挂载的覆盖只为 VPS 那个 IP 信任迁移 CA。
                release_config = module / "src" / "ipRelease" / "res" / "xml" / "network_security_config.xml"
                self.assertTrue(release_config.is_file(), "缺少 IP 入口的网络安全配置覆盖")
                # 且必须由 opt-in 条件挂载，不得常驻 release，否则正式域名包也会打进私有 CA。
                self.assertIn(
                    "sourceSets.release.res.srcDir 'src/ipRelease/res'",
                    (module / "build.gradle").read_text(encoding="utf-8"),
                )
                self.assertIn(
                    "if (otvIpReleaseOptIn)",
                    (module / "build.gradle").read_text(encoding="utf-8"),
                )
                self.assertFalse(
                    (module / "src" / "release" / "res" / "xml" / "network_security_config.xml").exists(),
                    "不得存在常驻 src/release 网络安全配置覆盖",
                )
                release_root = ElementTree.parse(release_config).getroot()
                release_hosts = [
                    block for block in release_root.findall("domain-config")
                    if block.findtext("domain") == "103.236.97.137"
                ]
                self.assertEqual(len(release_hosts), 1, "Release 覆盖必须恰好为 VPS IP 保留一段 domain-config")
                self.assertIn(
                    "@raw/mytv_migration_ca",
                    [item.attrib.get("src")
                     for item in release_hosts[0].findall("trust-anchors/certificates")],
                )

if __name__ == "__main__":
    unittest.main()

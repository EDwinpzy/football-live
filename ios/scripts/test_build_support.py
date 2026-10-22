import pathlib
import plistlib
import tempfile
import unittest
import zipfile

from build_cert_profile import build_profile
from package_ipa import default_output_path, package


class BuildSupportTests(unittest.TestCase):
    def make_app(self, root, platform):
        app = pathlib.Path(root) / "FootballLive.app"
        app.mkdir()
        (app / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": "FootballLive", "CFBundleSupportedPlatforms": [platform],
        }))
        (app / "FootballLive").write_bytes(b"test executable")
        (app / "Assets.car").write_bytes(b"test asset")
        return app

    def test_ipa_has_installable_layout_and_executable_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            app = self.make_app(root, "iPhoneOS")
            output = pathlib.Path(root) / "app.ipa"
            package(app, output)
            with zipfile.ZipFile(output) as archive:
                self.assertIn("Payload/FootballLive.app/Info.plist", archive.namelist())
                entry = archive.getinfo("Payload/FootballLive.app/FootballLive")
                self.assertEqual((entry.external_attr >> 16) & 0o777, 0o755)
                self.assertEqual(archive.read(entry), b"test executable")

    def test_default_ipa_name_is_football_live(self):
        with tempfile.TemporaryDirectory() as root:
            app = self.make_app(root, "iPhoneOS")
            self.assertEqual(default_output_path(app), app.parent / "FootballLive-unsigned.ipa")

    def test_simulator_product_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            app = self.make_app(root, "iPhoneSimulator")
            with self.assertRaisesRegex(ValueError, "simulator"):
                package(app, pathlib.Path(root) / "app.ipa")

    def test_missing_executable_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            app = self.make_app(root, "iPhoneOS")
            (app / "FootballLive").unlink()
            with self.assertRaisesRegex(ValueError, "executable"):
                package(app, pathlib.Path(root) / "app.ipa")

    def test_certificate_profile_only_installs_removable_public_root(self):
        der = b"public certificate bytes"
        profile = build_profile(der)
        self.assertFalse(profile["PayloadRemovalDisallowed"])
        self.assertEqual(profile["PayloadIdentifier"], "com.qiubo.mytv.ios.ca")
        payload, = profile["PayloadContent"]
        self.assertEqual(payload["PayloadType"], "com.apple.security.root")
        self.assertEqual(payload["PayloadIdentifier"], "com.qiubo.mytv.ios.ca.certificate")
        self.assertEqual(payload["PayloadContent"], der)
        self.assertEqual(payload["PayloadCertificateFileName"], "FootballLive-Root-CA.cer")
        self.assertEqual(payload["PayloadDisplayName"], "足球直播根证书")
        self.assertEqual(profile["PayloadDisplayName"], "足球直播 HTTPS 证书")
        self.assertNotIn("Password", payload)
        self.assertEqual(plistlib.loads(plistlib.dumps(profile)), profile)


if __name__ == "__main__":
    unittest.main()

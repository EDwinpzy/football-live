import pathlib
import plistlib
import tempfile
import unittest
import zipfile

from build_cert_profile import build_profile
from package_ipa import package


class BuildSupportTests(unittest.TestCase):
    def make_app(self, root, platform):
        app = pathlib.Path(root) / "MyTV.app"
        app.mkdir()
        (app / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": "MyTV", "CFBundleSupportedPlatforms": [platform],
        }))
        (app / "MyTV").write_bytes(b"test executable")
        (app / "Assets.car").write_bytes(b"test asset")
        return app

    def test_ipa_has_installable_layout_and_executable_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            app = self.make_app(root, "iPhoneOS")
            output = pathlib.Path(root) / "app.ipa"
            package(app, output)
            with zipfile.ZipFile(output) as archive:
                self.assertIn("Payload/MyTV.app/Info.plist", archive.namelist())
                entry = archive.getinfo("Payload/MyTV.app/MyTV")
                self.assertEqual((entry.external_attr >> 16) & 0o777, 0o755)
                self.assertEqual(archive.read(entry), b"test executable")

    def test_simulator_product_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            app = self.make_app(root, "iPhoneSimulator")
            with self.assertRaisesRegex(ValueError, "simulator"):
                package(app, pathlib.Path(root) / "app.ipa")

    def test_missing_executable_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            app = self.make_app(root, "iPhoneOS")
            (app / "MyTV").unlink()
            with self.assertRaisesRegex(ValueError, "executable"):
                package(app, pathlib.Path(root) / "app.ipa")

    def test_certificate_profile_only_installs_removable_public_root(self):
        der = b"public certificate bytes"
        profile = build_profile(der)
        self.assertFalse(profile["PayloadRemovalDisallowed"])
        payload, = profile["PayloadContent"]
        self.assertEqual(payload["PayloadType"], "com.apple.security.root")
        self.assertEqual(payload["PayloadContent"], der)
        self.assertNotIn("Password", payload)
        self.assertEqual(plistlib.loads(plistlib.dumps(profile)), profile)


if __name__ == "__main__":
    unittest.main()

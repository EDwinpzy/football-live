import hashlib
import importlib.util
import os
import ssl
import urllib.request
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATION_ORIGIN = "https://103.236.97.137:35208"
ENVIRONMENT_KEYS = (
    "OTV_CLOUD_BASE",
    "OTV_LICENSE_ENDPOINT",
    "OTV_IPTV_LATEST_URL",
    "OTV_APK_UPDATE_OBJECT_BASE",
)


class EmbeddedBackendMigrationConfigTests(unittest.TestCase):
    def _load_runner(self, app_dir):
        path = app_dir / "src" / "main" / "python" / "proxy_runner.py"
        spec = importlib.util.spec_from_file_location(f"proxy_runner_{app_dir.name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_embedded_python_uses_the_apk_origin_for_every_cloud_route_and_adds_ca(self):
        for app_dir in (ROOT / "android" / "app", ROOT / "internal" / "mobile" / "app"):
            with self.subTest(app=app_dir):
                runner = self._load_runner(app_dir)
                ca_path = app_dir / "src" / "main" / "res" / "raw" / "mytv_migration_ca.pem"
                original_env = {key: os.environ.get(key) for key in ENVIRONMENT_KEYS}
                previous_opener = urllib.request._opener
                system_cas = set(ssl.create_default_context().get_ca_certs(binary_form=True))
                try:
                    with self.assertRaisesRegex(ValueError, "required"):
                        runner.start(str(app_dir), 8090)
                    runner.configure_api_environment(MIGRATION_ORIGIN, str(ca_path))
                    self.assertEqual(os.environ["OTV_CLOUD_BASE"], MIGRATION_ORIGIN + "/web")
                    self.assertEqual(os.environ["OTV_LICENSE_ENDPOINT"], MIGRATION_ORIGIN + "/activate")
                    self.assertEqual(os.environ["OTV_IPTV_LATEST_URL"], MIGRATION_ORIGIN + "/iptv/latest")
                    self.assertEqual(
                        os.environ["OTV_APK_UPDATE_OBJECT_BASE"],
                        MIGRATION_ORIGIN + "/v1/storages/object/hotupdate",
                    )
                    handler = next(
                        item for item in urllib.request._opener.handlers
                        if isinstance(item, urllib.request.HTTPSHandler)
                    )
                    self.assertTrue(handler._context.check_hostname)
                    self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
                    loaded_cas = set(handler._context.get_ca_certs(binary_form=True))
                    pem = ca_path.read_text(encoding="ascii")
                    migration_ca_der = ssl.PEM_cert_to_DER_cert(pem)
                    self.assertIn(
                        hashlib.sha256(migration_ca_der).digest(),
                        {hashlib.sha256(cert).digest() for cert in loaded_cas},
                        "migration CA was not installed in Python TLS trust",
                    )
                    self.assertTrue(system_cas.issubset(loaded_cas))
                finally:
                    urllib.request.install_opener(previous_opener)
                    for key, value in original_env.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.db_import import load_auth_credentials, load_endpoint_config


class SelfHostedAdminConfigTests(unittest.TestCase):
    def test_selfhosted_endpoints_are_private_and_never_default_to_cloudbase(self):
        config = load_endpoint_config({
            "OTV_SELFHOSTED_MODE": "1",
            "OTV_PUBLIC_BASE_URL": "https://tv.example.org",
        })
        self.assertEqual(config.auth_url, "http://auth:8081/auth/v1/signin")
        self.assertEqual(config.rest_base, "http://postgrest:3000")
        self.assertEqual(config.storage_base, "http://storage:8092/v1/storages/object/hotupdate")
        self.assertEqual(config.hotupdate_endpoint, "http://business-api:8082/hotupdate")
        self.assertEqual(config.activate_endpoint, "http://business-api:8082/activate")
        self.assertEqual(config.public_base_url, "https://tv.example.org")
        config.check_url("http://storage:8092/v1/storages/object/hotupdate/iptv/latest.m3u?x-upsert=true")
        with self.assertRaisesRegex(ValueError, "allowlist"):
            config.check_url("https://appletv.example.tcloudbasegateway.com/v1/rdb/rest/rpc/activate_code")
        with self.assertRaisesRegex(ValueError, "allowlist"):
            config.check_url("http://169.254.169.254/latest/meta-data")


    def test_public_download_urls_use_the_new_origin_and_private_uploads_use_storage_service(self):
        config = load_endpoint_config({
            "OTV_SELFHOSTED_MODE": "1",
            "OTV_PUBLIC_BASE_URL": "https://tv.example.org:34567",
        })
        self.assertEqual(
            config.public_object_url("hotupdate", "apk-update/tv package.zip"),
            "https://tv.example.org:34567/v1/storages/object/hotupdate/apk-update/tv%20package.zip",
        )
        self.assertEqual(
            config.storage_object_url("apk-update/tv package.zip"),
            "http://storage:8092/v1/storages/object/hotupdate/apk-update/tv%20package.zip",
        )
        self.assertEqual(
            config.public_web_url("apk-update/tv.json"),
            "https://tv.example.org:34567/web/apk-update/tv.json",
        )

    def test_selfhosted_public_origin_must_be_https_and_not_cloudbase(self):
        for origin in ("http://tv.example.org", "https://tv.example.tcloudbase.com"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                load_endpoint_config({"OTV_SELFHOSTED_MODE": "1", "OTV_PUBLIC_BASE_URL": origin})

    def test_auth_credentials_can_be_read_from_a_root_only_runtime_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api-client.json"
            path.write_text(json.dumps({"username": "mytv-api", "password": "runtime-secret"}), encoding="utf-8")
            if os.name == "posix":
                path.chmod(0o600)
            user, password = load_auth_credentials({"OTV_AUTH_CREDENTIALS_FILE": str(path)})
        self.assertEqual((user, password), ("mytv-api", "runtime-secret"))

    def test_auth_credential_file_rejects_group_or_world_access(self):
        if os.name != "posix":
            self.skipTest("POSIX file mode check")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api-client.json"
            path.write_text(json.dumps({"username": "mytv-api", "password": "runtime-secret"}), encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "permissions"):
                load_auth_credentials({"OTV_AUTH_CREDENTIALS_FILE": str(path)})


if __name__ == "__main__":
    unittest.main()

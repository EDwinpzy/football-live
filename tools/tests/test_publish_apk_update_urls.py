import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import db_import  # noqa: E402
import publish_apk_update  # noqa: E402
os.environ.setdefault("OTV_ADMIN_USER", "migration-test")
os.environ.setdefault("OTV_ADMIN_PASS", "migration-test-password")
import admin_server  # noqa: E402

from tools.selfhosted.auth_service import make_access_token  # noqa: E402
from tools.selfhosted.storage_service import make_server  # noqa: E402


def endpoint_config(*, storage_base, public_base, public_web_base, self_hosted):
    return db_import.EndpointConfig(
        base_url="http://postgrest:3000" if self_hosted else "https://cloudbase.example",
        auth_url="http://auth:8081/auth/v1/signin" if self_hosted else "https://cloudbase.example/auth",
        rest_base="http://postgrest:3000" if self_hosted else "https://cloudbase.example/rest",
        storage_base=storage_base,
        public_base_url=public_base,
        public_web_base_url=public_web_base,
        hotupdate_endpoint="http://business-api:8082/hotupdate" if self_hosted else "https://cloudbase.example/hotupdate",
        activate_endpoint="http://business-api:8082/activate" if self_hosted else "https://cloudbase.example/activate",
        environment_name="mytv-selfhosted" if self_hosted else "cloudbase",
        self_hosted=self_hosted,
    )


class PublishApkUpdateUrlTests(unittest.TestCase):
    def test_self_hosted_download_and_upload_urls_use_different_origins(self):
        endpoints = endpoint_config(
            storage_base="http://storage:8092/v1/storages/object/hotupdate",
            public_base="https://103.236.97.137:35208",
            public_web_base="https://103.236.97.137:35208",
            self_hosted=True,
        )
        key = "apk-update/tv/v110/parts/part-01-of-02"
        with patch.object(publish_apk_update.db_import, "ENDPOINTS", endpoints):
            self.assertEqual(
                publish_apk_update.obj_url(key),
                "https://103.236.97.137:35208/v1/storages/object/hotupdate/" + key,
            )
            self.assertEqual(
                publish_apk_update.upload_object_url(key),
                "http://storage:8092/v1/storages/object/hotupdate/" + key,
            )
            endpoints.check_url(publish_apk_update.upload_object_url(key))
            self.assertEqual(
                publish_apk_update.manifest_url("tv"),
                "https://103.236.97.137:35208/web/apk-update/tv.json",
            )

    def test_cloudbase_download_and_upload_stay_on_cloudbase_storage(self):
        endpoints = endpoint_config(
            storage_base=f"https://{db_import.CLOUDBASE_API_HOST}/v1/storages/object/hotupdate",
            public_base=f"https://{db_import.CLOUDBASE_API_HOST}",
            public_web_base=f"https://{db_import.CLOUDBASE_SERVICE_HOST}",
            self_hosted=False,
        )
        key = "apk-update/mobile/v110/parts/part-01-of-02"
        expected = f"https://{db_import.CLOUDBASE_API_HOST}/v1/storages/object/hotupdate/" + key
        with patch.object(publish_apk_update.db_import, "ENDPOINTS", endpoints):
            self.assertEqual(publish_apk_update.obj_url(key), expected)
            self.assertEqual(publish_apk_update.upload_object_url(key), expected)
            endpoints.check_url(publish_apk_update.upload_object_url(key))
            self.assertEqual(
                publish_apk_update.manifest_url("mobile"),
                f"https://{db_import.CLOUDBASE_SERVICE_HOST}/web/apk-update/mobile.json",
            )

    def test_self_hosted_admin_and_apk_publishers_upload_using_storage_api_method(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secret = b"m" * 48
            server = make_server("127.0.0.1", 0, root, {"hotupdate"}, {"2094843486709620737"}, secret)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                endpoints = endpoint_config(
                    storage_base=f"http://127.0.0.1:{server.server_port}/v1/storages/object/hotupdate",
                    public_base="https://103.236.97.137:35208",
                    public_web_base="https://103.236.97.137:35208",
                    self_hosted=True,
                )
                token = make_access_token("2094843486709620737", secret)
                with (
                    patch.object(db_import, "ENDPOINTS", endpoints),
                    patch.object(db_import, "check_url"),
                    patch.object(admin_server, "cloud_token", return_value=token),
                ):
                    publish_apk_update.put_object(
                        "apk-update/tv/test.apk", b"apk-bytes", "application/octet-stream", token
                    )
                    admin_server.pg_put("iptv/test.m3u", b"#EXTM3U\n", "audio/x-mpegurl")
                self.assertEqual((root / "hotupdate/apk-update/tv/test.apk").read_bytes(), b"apk-bytes")
                self.assertEqual((root / "hotupdate/iptv/test.m3u").read_bytes(), b"#EXTM3U\n")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_cloudbase_apk_publisher_keeps_the_cloudbase_upload_method(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"{}"

        class Opener:
            def __init__(self):
                self.method = None

            def open(self, request, timeout):
                self.method = request.get_method()
                return Response()

        endpoints = endpoint_config(
            storage_base=f"https://{db_import.CLOUDBASE_API_HOST}/v1/storages/object/hotupdate",
            public_base=f"https://{db_import.CLOUDBASE_API_HOST}",
            public_web_base=f"https://{db_import.CLOUDBASE_SERVICE_HOST}",
            self_hosted=False,
        )
        opener = Opener()
        with (
            patch.object(db_import, "ENDPOINTS", endpoints),
            patch.object(db_import, "check_url"),
            patch.object(publish_apk_update.urllib.request, "build_opener", return_value=opener),
        ):
            publish_apk_update.put_object("apk-update/tv/test.apk", b"apk", "application/octet-stream", "token")
        self.assertEqual(opener.method, "POST")

    def test_iptv_writer_stops_before_storage_write_when_lease_is_lost(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secret = b"l" * 48
            server = make_server("127.0.0.1", 0, root, {"hotupdate"}, {"2094843486709620737"}, secret)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                endpoints = endpoint_config(
                    storage_base=f"http://127.0.0.1:{server.server_port}/v1/storages/object/hotupdate",
                    public_base="https://103.236.97.137:35208",
                    public_web_base="https://103.236.97.137:35208",
                    self_hosted=True,
                )
                token = make_access_token("2094843486709620737", secret)
                lost = threading.Event()

                class Lease:
                    def assert_valid(self):
                        if lost.is_set():
                            raise RuntimeError("IPTV publish lease was lost")

                lease = Lease()

                class ControlledLock:
                    @contextmanager
                    def hold(self):
                        yield lease

                def publish(_data):
                    lost.set()
                    admin_server.pg_put("iptv/must-not-write.m3u", b"#EXTM3U\n", "audio/x-mpegurl")
                    return {"ok": True}

                with (
                    patch.object(admin_server, "SELFHOSTED", True),
                    patch.object(admin_server.db_import, "ENDPOINTS", endpoints),
                    patch.object(admin_server.db_import, "check_url"),
                    patch.object(admin_server, "cloud_token", return_value=token),
                    patch("selfhosted.scheduler.DatabasePublishLock", return_value=ControlledLock()),
                ):
                    result = admin_server._guard_iptv_publisher(publish)({})

                self.assertIn("error", result)
                self.assertFalse((root / "hotupdate/iptv/must-not-write.m3u").exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

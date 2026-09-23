"""热更上传收尾时，暂存分片必须走私有 storage API 删除。

自托管迁移后公开对象地址是 VPS 的 HTTPS origin，不在 `check_url` 白名单里；
早期实现用公开地址删除分片，导致 `/api/hu_upload_commit` 抛 ValueError → Nginx 502，
热更发布整条链路作废（2026-09-24 实测）。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SELF_HOSTED_ENV = {
    "OTV_SELFHOSTED_MODE": "1",
    "OTV_PUBLIC_BASE_URL": "https://103.236.97.137:35208",
    "OTV_STORAGE_BASE": "http://storage:8092/v1/storages/object/hotupdate",
    "OTV_AUTH_URL": "http://auth:8081/auth/v1/signin",
    "OTV_REST_BASE": "http://postgrest:3000",
    "OTV_HOTUPDATE_ENDPOINT": "http://business-api:8082/hotupdate",
    "OTV_ACTIVATE_ENDPOINT": "http://business-api:8082/activate",
}
PRIVATE_PREFIX = "http://storage:8092/v1/storages/object/hotupdate/"


class _FakeResponse:
    def read(self, *_args, **_kwargs):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class AdminHotUpdateDeleteUrlTests(unittest.TestCase):
    def setUp(self):
        import admin_server  # noqa: E402  (tools/admin_server.py)
        import db_import  # noqa: E402  (tools/db_import.py)

        self.admin_server = admin_server
        self.db_import = db_import
        self._saved_endpoints = db_import.ENDPOINTS
        db_import.ENDPOINTS = db_import.load_endpoint_config(SELF_HOSTED_ENV)
        self.addCleanup(self._restore)

    def _restore(self):
        self.db_import.ENDPOINTS = self._saved_endpoints

    def _capture(self):
        captured = {}

        def fake_opener(*_handlers):
            class _Opener:
                def open(self, request, timeout=None):
                    captured["url"] = request.full_url
                    captured["method"] = request.get_method()
                    return _FakeResponse()

            return _Opener()

        patcher = mock.patch.object(self.admin_server.urllib.request, "build_opener", fake_opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        token = mock.patch.object(self.admin_server, "cloud_token", lambda: "test-token")
        token.start()
        self.addCleanup(token.stop)
        return captured

    def test_staging_chunk_cleanup_uses_private_storage_api(self):
        captured = self._capture()
        self.admin_server._hu_obj_delete("hu-uploads/abc/part-0000")
        self.assertEqual(captured.get("method"), "DELETE")
        self.assertTrue(captured.get("url", "").startswith(PRIVATE_PREFIX),
                        f"expected private storage URL, got {captured.get('url')!r}")

    def test_hotupdate_delete_removes_object_through_private_storage_api(self):
        captured = self._capture()
        with mock.patch.object(self.admin_server, "rpc", lambda *_a, **_k: {"ok": True}):
            self.admin_server.h_hu_delete({"version": 21})
        self.assertEqual(captured.get("method"), "DELETE")
        self.assertTrue(captured.get("url", "").startswith(PRIVATE_PREFIX),
                        f"expected private storage URL, got {captured.get('url')!r}")
        self.assertTrue(captured.get("url", "").endswith("backend-hu21.zip"))


if __name__ == "__main__":
    unittest.main()

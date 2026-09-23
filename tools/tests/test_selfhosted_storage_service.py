import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tools.selfhosted.auth_service import make_access_token
from tools.selfhosted.storage_service import make_server


class SelfHostedStorageServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "hotupdate" / "apk-update").mkdir(parents=True)
        (self.root / "hotupdate" / "apk-update" / "test.bin").write_bytes(b"0123456789")
        self.secret = b"s" * 48
        self.server = make_server(
            "127.0.0.1", 0, self.root, {"hotupdate"}, {"2094843486709620737"}, self.secret
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_get_supports_single_byte_ranges_and_head(self):
        request = urllib.request.Request(
            self.base + "/v1/storages/object/hotupdate/apk-update/test.bin",
            headers={"Range": "bytes=2-5"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"2345")
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")

        head = urllib.request.Request(
            self.base + "/v1/storages/object/hotupdate/apk-update/test.bin", method="HEAD"
        )
        with urllib.request.urlopen(head) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Length"], "10")
            self.assertEqual(response.read(), b"")

    def test_rejects_path_traversal_and_unsatisfiable_ranges(self):
        traversal = urllib.request.Request(
            self.base + "/v1/storages/object/hotupdate/%2e%2e/private"
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(traversal)
        self.assertEqual(caught.exception.code, 404)

        request = urllib.request.Request(
            self.base + "/v1/storages/object/hotupdate/apk-update/test.bin",
            headers={"Range": "bytes=50-60"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 416)
        self.assertEqual(caught.exception.headers["Content-Range"], "bytes */10")

    def test_upload_requires_admin_token_and_delete_uses_same_gate(self):
        url = self.base + "/v1/storages/object/hotupdate/apk-update/upload.bin"
        anonymous = urllib.request.Request(url, data=b"private", method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(anonymous)
        self.assertEqual(caught.exception.code, 401)

        token = make_access_token("2094843486709620737", self.secret)
        request = urllib.request.Request(
            url,
            data=b"private",
            method="PUT",
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["sha256"], hashlib.sha256(b"private").hexdigest())
        self.assertEqual((self.root / "hotupdate" / "apk-update" / "upload.bin").read_bytes(), b"private")

        delete = urllib.request.Request(url, method="DELETE", headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(delete) as response:
            self.assertEqual(response.status, 204)
        self.assertFalse((self.root / "hotupdate" / "apk-update" / "upload.bin").exists())


if __name__ == "__main__":
    unittest.main()

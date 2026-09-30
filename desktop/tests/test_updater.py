"""Windows EXE 自动更新（热更 zip）测试。"""
import hashlib
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

UPDATER_PATH = Path(__file__).resolve().parents[1] / "updater.py"


def _load_updater():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mytv_desktop_updater", UPDATER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


updater = _load_updater()


def _zip_bytes(marker="v2"):
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("proxy.py", "# %s\n" % marker)
        zf.writestr("www/index.html", "<html>%s</html>" % marker)
        zf.writestr("www/app.js", "// %s" % marker)
    return buf.getvalue()


def _info(version, payload, key="backend-hu9.zip", sha=None):
    return updater.UpdateInfo(
        version=version,
        url="https://x/y.zip",
        sha256=sha or hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        object_key=key)


class PathTests(unittest.TestCase):
    def test_update_root_is_under_local_app_data(self):
        root = updater.update_root(Path(r"C:\Users\demo\AppData\Local\MyTV"))
        self.assertEqual(root.name, "update")

    def test_device_id_matches_the_server_sha256_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev = updater.device_id(Path(tmp) / "MyTV")
        # 服务端 api_service.py 用 [a-f0-9]{64} 校验，任何别的形状都会被
        # 判 bad_device —— 2026-09-30 上线实测踩过这个坑。
        self.assertEqual(len(dev), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in dev), dev)

    def test_device_id_is_stable_across_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "MyTV"
            first = updater.device_id(root)
            second = updater.device_id(root)
        self.assertEqual(first, second)

    def test_device_id_replaces_a_corrupt_cached_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "MyTV"
            root.mkdir(parents=True)
            (root / "device-id").write_text("desktop", encoding="ascii")
            dev = updater.device_id(root)
        self.assertEqual(len(dev), 64)

    def test_pending_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "update"
            self.assertIsNone(updater.read_pending(root))
            updater.write_pending(root, 7, "a" * 64, "backend-hu7.zip")
            self.assertEqual(
                updater.read_pending(root), (7, "a" * 64, "backend-hu7.zip"))

    def test_corrupt_pending_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "update"
            root.mkdir(parents=True)
            (root / "pending.json").write_text("{bad", encoding="utf-8")
            self.assertIsNone(updater.read_pending(root))


class ManifestTests(unittest.TestCase):
    def test_no_update(self):
        payload = {"ret": 0, "hasUpdate": False}
        with mock.patch.object(updater, "post_check", return_value=payload):
            r = updater.check_for_update("https://tv.example", 3, 1, "dev")
        self.assertFalse(r.update_available)
        self.assertIsNone(r.version)

    def test_blackout_is_no_update(self):
        payload = {"ret": 0, "hasUpdate": False, "blackout": True}
        with mock.patch.object(updater, "post_check", return_value=payload):
            r = updater.check_for_update("https://tv.example", 1, 1, "dev")
        self.assertFalse(r.update_available)

    def test_parses_package(self):
        payload = {"ret": 0, "hasUpdate": True,
                   "pkg": {"objectKey": "backend-hu21.zip",
                           "sha256": "b" * 64, "size": 1234}}
        with mock.patch.object(updater, "post_check", return_value=payload):
            r = updater.check_for_update("https://tv.example", 3, 1, "dev")
        self.assertTrue(r.update_available)
        self.assertEqual(r.version, 21)
        self.assertEqual(r.sha256, "b" * 64)
        self.assertEqual(r.size, 1234)
        self.assertEqual(
            r.url,
            "https://tv.example/v1/storages/object/hotupdate/backend-hu21.zip")

    def test_reads_the_real_server_file_size_field(self):
        # 服务端线上实际下发的是 fileSize（2026-09-30 实测 payload），
        # 早期只认 size 会让包大小校验被静默跳过。
        payload = {"ret": 0, "hasUpdate": True,
                   "pkg": {"objectKey": "backend-hu29.zip",
                           "sha256": "c" * 64, "fileSize": 40890308}}
        with mock.patch.object(updater, "post_check", return_value=payload):
            r = updater.check_for_update("https://tv.example", 0, 120, "d" * 64)
        self.assertEqual(r.size, 40890308)
        self.assertEqual(r.version, 29)

    def test_reads_hot_version_code_when_the_key_has_no_digits(self):
        payload = {"ret": 0, "hasUpdate": True,
                   "pkg": {"objectKey": "backend.zip",
                           "sha256": "d" * 64, "fileSize": 10},
                   "hotVersionCode": 42}
        with mock.patch.object(updater, "post_check", return_value=payload):
            r = updater.check_for_update("https://tv.example", 0, 120, "d" * 64)
        self.assertEqual(r.version, 42)

    def test_rejects_non_https(self):
        with self.assertRaises(ValueError):
            updater.check_for_update("http://tv.example", 1, 1, "dev")

    def test_rejects_missing_sha256(self):
        payload = {"ret": 0, "hasUpdate": True,
                   "pkg": {"objectKey": "k.zip", "url": "https://x/y.zip"}}
        with mock.patch.object(updater, "post_check", return_value=payload):
            with self.assertRaises(ValueError):
                updater.check_for_update("https://tv.example", 1, 1, "dev")


class DownloadTests(unittest.TestCase):
    def test_stages_verified_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "update"
            payload = _zip_bytes("v9")
            with mock.patch.object(updater, "download_file", return_value=payload):
                staged = updater.stage_update(root, _info(9, payload))
            self.assertTrue((staged / "proxy.py").is_file())
            self.assertEqual(
                (staged / "www" / "app.js").read_text("utf-8"), "// v9")
            self.assertEqual(
                updater.read_pending(root),
                (9, hashlib.sha256(payload).hexdigest(), "backend-hu9.zip"))

    def test_corrupt_download_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "update"
            payload = _zip_bytes("tampered")
            info = _info(9, payload, sha="c" * 64)
            with mock.patch.object(updater, "download_file", return_value=payload):
                with self.assertRaises(ValueError):
                    updater.stage_update(root, info)
            self.assertIsNone(updater.read_pending(root))
            self.assertFalse((root / "backend-hu9.zip").exists())

    def test_zip_without_web_assets_rejected(self):
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("proxy.py", "# no web\n")
        payload = buf.getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "update"
            with mock.patch.object(updater, "download_file", return_value=payload):
                with self.assertRaises(ValueError):
                    updater.stage_update(root, _info(3, payload, key="k.zip"))
            self.assertIsNone(updater.read_pending(root))

    def test_traversal_zip_rejected(self):
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("proxy.py", "# ok\n")
            zf.writestr("www/index.html", "<html></html>")
            zf.writestr("../escape.txt", "pwned")
        payload = buf.getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "update"
            with mock.patch.object(updater, "download_file", return_value=payload):
                with self.assertRaises(ValueError):
                    updater.stage_update(root, _info(4, payload, key="k.zip"))
            self.assertIsNone(updater.read_pending(root))


class ResolveTests(unittest.TestCase):
    def _stage(self, root, key="backend-hu9"):
        staged = root / key
        (staged / "www").mkdir(parents=True)
        (staged / "proxy.py").write_text("# v9", encoding="utf-8")
        (staged / "www" / "app.js").write_text("// v9", encoding="utf-8")
        (staged / ".extracted").write_text("ok", encoding="ascii")
        updater.write_pending(
            root, 9, hashlib.sha256(b"x").hexdigest(), key + ".zip")
        return staged

    def test_prefers_valid_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "update"
            staged = self._stage(root, key="backend-hu9")
            resolved, is_update = updater.resolve_backend_dir(
                root, built_in=Path(r"C:\nowhere\backend.zip"))
            self.assertTrue(is_update)
            self.assertEqual(resolved, staged)

    def test_falls_back_on_broken_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "update"
            updater.write_pending(root, 9, "d" * 64, "backend-hu9.zip")
            builtin = Path(tmp) / "builtin.zip"
            builtin.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            resolved, is_update = updater.resolve_backend_dir(root, built_in=builtin)
            self.assertFalse(is_update)
            self.assertEqual(resolved, builtin)

    def test_falls_back_without_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "update"
            builtin = Path(tmp) / "builtin.zip"
            builtin.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            resolved, is_update = updater.resolve_backend_dir(root, built_in=builtin)
            self.assertFalse(is_update)
            self.assertEqual(resolved, builtin)


class NetworkTests(unittest.TestCase):
    def test_post_check_payload(self):
        captured = {}

        class FakeResponse:
            status = 200

            def read(self, *_a):
                return json.dumps({"ret": 0, "hasUpdate": False}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            return FakeResponse()

        fake_opener = mock.Mock()
        fake_opener.open = fake_open
        with mock.patch.object(updater, "direct_opener", return_value=fake_opener):
            updater.post_check("https://tv.example", 4, 2, "dev123")
        self.assertEqual(captured["url"], "https://tv.example/hotupdate/check")
        self.assertEqual(
            json.loads(captured["data"].decode("utf-8")),
            {"hotVersion": 4, "apkVersion": 2, "deviceId": "dev123"})

    def test_network_error_propagates(self):
        def boom(*_a, **_k):
            raise urllib.error.URLError("down")

        fake_opener = mock.Mock()
        fake_opener.open = boom
        with mock.patch.object(updater, "direct_opener", return_value=fake_opener):
            with self.assertRaises(Exception):
                updater.post_check("https://tv.example", 1, 1, "dev")

    def test_direct_opener_ignores_the_system_proxy(self):
        # 2026-09-30 实测：本机系统代理 127.0.0.1:4210（Clash）会拦掉
        # 到裸 IP 的热更请求，直连才 200。热更必须绕过系统代理。
        # build_opener 会消费传入的 ProxyHandler：proxies 为空 dict 时它直接
        # 把自己从 opener 里摘掉，所以「handlers 里没有 ProxyHandler」就是
        # 「不走代理」的证据。
        opener = updater.direct_opener()
        self.addCleanup(opener.close)
        self.assertFalse([h for h in opener.handlers
                          if isinstance(h, urllib.request.ProxyHandler)])

    def test_direct_opener_keeps_the_supplied_ca_context(self):
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        opener = updater.direct_opener(ctx)
        self.assertTrue(any(isinstance(h, urllib.request.HTTPSHandler)
                            for h in opener.handlers))


if __name__ == "__main__":
    unittest.main()

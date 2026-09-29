import io
import os
import ssl
import sys
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock


LAUNCHER_PATH = Path(__file__).resolve().parents[1] / "launcher.py"

# 仅用于测试 TLS 信任装配的自签 CA 公钥；产品源码不携带任何证书。
TEST_CA_PEM = b"""-----BEGIN CERTIFICATE-----
MIIBhTCCASugAwIBAgIQIRi6zePL6mKjOipn+dNuaTAKBggqhkjOPQQDAjASMRAw
DgYDVQQKEwdBY21lIENvMB4XDTE3MTAyMDE5NDMwNloXDTE4MTAyMDE5NDMwNlow
EjEQMA4GA1UEChMHQWNtZSBDbzBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABD0d
7VNhbWvZLWPuj/RtHFjvtJBEwOkhbN/BnnE8rnZR8+sbwnc/KhCk3FhnpHZnQz7B
5aETbbIgmuvewdjvSBSjYzBhMA4GA1UdDwEB/wQEAwICpDATBgNVHSUEDDAKBggr
BgEFBQcDATAPBgNVHRMBAf8EBTADAQH/MCkGA1UdEQQiMCCCDmxvY2FsaG9zdDo1
NDUzgg4xMjcuMC4wLjE6NTQ1MzAKBggqhkjOPQQDAgNIADBFAiEA2zpJEPQyz6/l
Wf86aX6PepsntZv2GYlA5UpabfT2EZICICpJ5h/iI+i341gBmLiAFQOyTDT+/wQc
6MF9+Yw1Yy0t
-----END CERTIFICATE-----
"""


def _load_launcher():
    import importlib.util

    spec = importlib.util.spec_from_file_location("mytv_desktop_launcher", LAUNCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


launcher = _load_launcher()


class LocalAppDataTests(unittest.TestCase):
    def test_local_app_data_root_follows_the_environment(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\demo\AppData\Local"}):
            self.assertEqual(
                launcher.local_app_data_root(),
                Path(r"C:\Users\demo\AppData\Local") / "MyTV",
            )

    def test_local_app_data_root_falls_back_to_userprofile_then_home(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": r"C:\Users\demo"}, clear=True):
            self.assertEqual(
                launcher.local_app_data_root(),
                Path(r"C:\Users\demo") / "MyTV",
            )

        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(launcher.Path, "home", return_value=Path(r"C:\Users\demo")):
            self.assertEqual(
                launcher.local_app_data_root(),
                Path(r"C:\Users\demo") / "MyTV",
            )


class SafeExtractionTests(unittest.TestCase):
    def _zip(self, path, members):
        with zipfile.ZipFile(path, "w") as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)

    def test_extracts_plain_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "backend.zip"
            self._zip(archive, {"proxy.py": "print(1)", "www/app.js": "console.log(1)"})
            target = launcher.extract_backend_archive(archive, root / "out")
            self.assertEqual((target / "proxy.py").read_text(encoding="utf-8"), "print(1)")
            self.assertEqual((target / "www" / "app.js").read_text(encoding="utf-8"), "console.log(1)")

    def test_rejects_absolute_member_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "backend.zip"
            self._zip(archive, {"../escape.py": "nope"})
            with self.assertRaisesRegex(ValueError, "unsafe"):
                launcher.extract_backend_archive(archive, root / "out")
            self.assertFalse((root / "escape.py").exists())

    def test_rejects_traversal_and_windows_absolute_members(self):
        for name in ("../escape.py", "nested/../../escape.py", "/abs/escape.py", "C:\\escape.py",
                     "C:escape.py", "C:folder/escape.py", "nested/C:escape.py"):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    archive = root / "backend.zip"
                    self._zip(archive, {name: "nope"})
                    with self.assertRaisesRegex(ValueError, "unsafe"):
                        launcher.extract_backend_archive(archive, root / "out")

    def test_rejects_symlink_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "backend.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                info = zipfile.ZipInfo("link.py")
                info.external_attr = (0o120777 << 16)
                zf.writestr(info, "target.py")
            with self.assertRaisesRegex(ValueError, "symlink"):
                launcher.extract_backend_archive(archive, root / "out")

    def test_leaves_no_partial_tree_when_a_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "backend.zip"
            self._zip(archive, {"good.py": "ok", "../bad.py": "no"})
            out = root / "out"
            with self.assertRaises(ValueError):
                launcher.extract_backend_archive(archive, out)
            self.assertFalse(out.exists())

    def test_completed_extraction_writes_a_marker_and_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "backend.zip"
            self._zip(archive, {"proxy.py": "print(1)"})
            target = root / "out"
            launcher.extract_backend_archive(archive, target)
            marker = target / ".extracted"
            self.assertTrue(marker.is_file())
            # 第二次解压到同一目标应幂等：先摘掉 marker 再触发一次重新解压。
            marker.unlink()
            launcher.extract_backend_archive(archive, target)
            self.assertTrue(marker.is_file())


class ArchiveResolutionTests(unittest.TestCase):
    def test_prefers_the_pyinstaller_data_file(self):
        meipass = Path("/tmp/_MEI123")
        with mock.patch.object(launcher, "is_frozen", return_value=True), \
                mock.patch.object(launcher, "bundle_root", return_value=meipass), \
                mock.patch.object(launcher, "repo_root", return_value=Path("/repo")):
            path = launcher.resolve_backend_archive()
            self.assertEqual(path, meipass / "desktop" / "backend.zip")

    def test_falls_back_to_the_repo_copy_in_development(self):
        repo = Path("/repo")
        with mock.patch.object(launcher, "is_frozen", return_value=False), \
                mock.patch.object(launcher, "repo_root", return_value=repo):
            self.assertEqual(launcher.resolve_backend_archive(), repo / "tools" / "backend-src")

    def test_development_mode_prefers_repo_sources_and_ignores_zip(self):
        repo = Path("/repo")
        with mock.patch.object(launcher, "is_frozen", return_value=False), \
                mock.patch.object(launcher, "repo_root", return_value=repo):
            self.assertEqual(launcher.resolve_backend_dir(), repo / "tools" / "backend-src")


class CaTrustTests(unittest.TestCase):
    def test_installs_private_ca_on_top_of_system_trust(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca = Path(tmp) / "ca.pem"
            ca.write_bytes(TEST_CA_PEM)
            original_env = {key: os.environ.get(key) for key in launcher.API_ENV_KEYS}
            previous_opener = urllib.request._opener
            system_cas = set(ssl.create_default_context().get_ca_certs(binary_form=True))
            try:
                launcher.configure_api_environment("https://example.test:35208", ca)
                self.assertEqual(os.environ["OTV_CLOUD_BASE"], "https://example.test:35208/web")
                self.assertEqual(os.environ["OTV_LICENSE_ENDPOINT"], "https://example.test:35208/activate")
                self.assertEqual(os.environ["OTV_IPTV_LATEST_URL"], "https://example.test:35208/iptv/latest")
                self.assertEqual(
                    os.environ["OTV_APK_UPDATE_OBJECT_BASE"],
                    "https://example.test:35208/v1/storages/object/hotupdate",
                )
                self.assertEqual(os.environ["OTV_METADATA_API_URL"], "https://example.test:35208")
                self.assertEqual(
                    os.environ["OTV_VOD_LINE_INDEX_URL"],
                    "https://example.test:35208/v1/storages/object/hotupdate/vod-lines.json",
                )
                handler = next(
                    item for item in urllib.request._opener.handlers
                    if isinstance(item, urllib.request.HTTPSHandler)
                )
                self.assertTrue(handler._context.check_hostname)
                self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
                loaded = set(handler._context.get_ca_certs(binary_form=True))
                self.assertTrue(system_cas.issubset(loaded))
                der = ssl.PEM_cert_to_DER_cert(ca.read_text(encoding="ascii"))
                import hashlib

                self.assertIn(
                    hashlib.sha256(der).digest(),
                    {hashlib.sha256(cert).digest() for cert in loaded},
                )
            finally:
                urllib.request.install_opener(previous_opener)
                for key, value in original_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_rejects_non_https_origins(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca = Path(tmp) / "ca.pem"
            ca.write_bytes(TEST_CA_PEM)
            for origin in ("http://example.test", "https://user:pw@example.test", "https://example.test/x"):
                with self.subTest(origin=origin):
                    with self.assertRaisesRegex(ValueError, "HTTPS origin"):
                        launcher.configure_api_environment(origin, ca)

    def test_missing_ca_file_is_fatal(self):
        with self.assertRaisesRegex(ValueError, "CA file is missing"):
            launcher.configure_api_environment("https://example.test", Path("nope.pem"))


class PortSelectionTests(unittest.TestCase):
    class _FakeSocket:
        """让 bind() 按需失败，以模拟端口占用。"""

        def __init__(self, busy_ports):
            self.busy_ports = busy_ports

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def bind(self, address):
            if address[1] in self.busy_ports:
                raise OSError(10048, "address already in use")

    def test_prefers_8090_when_free(self):
        with mock.patch.object(launcher.socket, "socket",
                               return_value=self._FakeSocket(busy_ports=set())):
            self.assertEqual(launcher.select_backend_port(preferred=8090), 8090)

    def test_falls_back_when_preferred_port_is_busy(self):
        with mock.patch.object(launcher.socket, "socket",
                               return_value=self._FakeSocket(busy_ports={8090})):
            self.assertEqual(launcher.select_backend_port(preferred=8090), 8091)

    def test_gives_up_when_every_candidate_is_busy(self):
        with mock.patch.object(launcher.socket, "socket",
                               return_value=self._FakeSocket(busy_ports={8090, 8091, 8092})):
            self.assertEqual(launcher.select_backend_port(preferred=8090), 8093)

    def test_raises_when_the_bounded_retry_budget_is_exhausted(self):
        with mock.patch.object(launcher.socket, "socket",
                               return_value=self._FakeSocket(busy_ports=set(range(8090, 8155)))):
            with self.assertRaises(launcher.BackendStartError):
                launcher.select_backend_port(preferred=8090)

    def test_never_returns_a_busy_port(self):
        with mock.patch.object(launcher.socket, "socket",
                               return_value=self._FakeSocket(busy_ports=set(range(8090, 8155)))):
            with self.assertRaises(launcher.BackendStartError):
                launcher.select_backend_port(preferred=8090)

    def test_real_probe_agrees_with_a_real_listener(self):
        """真起一个 listener，端口探测必须判定为占用。"""
        import socket as real_socket

        with real_socket.socket(real_socket.AF_INET, real_socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.assertFalse(launcher._port_is_free(port))


class HealthCheckTests(unittest.TestCase):
    def test_returns_true_for_a_health_payload(self):
        response = mock.Mock()
        response.status = 200
        response.read.return_value = b'{"ok": true}'
        response.__enter__ = lambda self: self
        response.__exit__ = lambda *a: False
        with mock.patch.object(launcher.urllib.request, "urlopen", return_value=response):
            self.assertTrue(launcher.wait_for_backend(8090, timeout=0.1, delay=0.0))

    def test_returns_false_on_timeout(self):
        with mock.patch.object(launcher.urllib.request, "urlopen", side_effect=OSError("nope")):
            self.assertFalse(launcher.wait_for_backend(8090, timeout=0.05, delay=0.0))


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_detects_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instance.lock"
            first = launcher.SingleInstance(path)
            self.assertTrue(first.acquire())
            second = launcher.SingleInstance(path)
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_stale_lock_does_not_block_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instance.lock"
            path.write_text(str(2 ** 31 - 1), encoding="ascii")
            guard = launcher.SingleInstance(path)
            self.assertTrue(guard.acquire())
            guard.release()


class WebviewStartTests(unittest.TestCase):
    def test_uses_edgechromium_with_a_persistent_profile(self):
        with mock.patch.object(launcher, "webview") as webview, \
                mock.patch.object(launcher.webview, "create_window") as create_window:
            window = mock.Mock()
            create_window.return_value = window
            launcher.show_window("http://127.0.0.1:8090/", storage_path=r"C:\profile", title="My TV")
        self.assertTrue(create_window.called)
        webview.start.assert_called_once()
        kwargs = webview.start.call_args.kwargs
        self.assertEqual(kwargs["gui"], "edgechromium")
        self.assertFalse(kwargs["private_mode"])
        self.assertEqual(kwargs["storage_path"], r"C:\profile")


class SelfTestModeTests(unittest.TestCase):
    def test_self_test_rejects_broken_quickjs_runtime(self):
        with mock.patch.object(launcher, "wait_for_backend", return_value=True), \
                mock.patch.object(launcher.urllib.request, "urlopen") as urlopen, \
                mock.patch.object(launcher, "quickjs_smoke", return_value=False, create=True):
            response = urlopen.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b"<html>My TV</html>"
            self.assertNotEqual(launcher.run_self_test(8090), 0)

    def test_self_test_validates_health_and_index(self):
        health = mock.Mock()
        health.status = 200
        health.read.return_value = b'{"ok": true}'
        health.__enter__ = lambda self: self
        health.__exit__ = lambda *a: False
        index = mock.Mock()
        index.status = 200
        index.read.return_value = b"<html>My TV</html>"
        index.__enter__ = lambda self: self
        index.__exit__ = lambda *a: False

        responses = {"http://127.0.0.1:8090/api/health": health, "http://127.0.0.1:8090/": index}
        with mock.patch.object(launcher.urllib.request, "urlopen", side_effect=lambda url, **kw: responses[url]):
            result = launcher.run_self_test(8090)
        self.assertEqual(result, 0)

    def test_self_test_fails_when_health_never_comes_up(self):
        with mock.patch.object(launcher, "wait_for_backend", return_value=False):
            self.assertNotEqual(launcher.run_self_test(8090), 0)


class FrozenExtractionTests(unittest.TestCase):
    """打包形态：backend.zip -> %LOCALAPPDATA%/MyTV/backend/<sha256>/。"""

    def test_frozen_mode_extracts_the_zip_into_a_sha256_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meipass = root / "_MEI"
            (meipass / "desktop").mkdir(parents=True)
            archive = meipass / "desktop" / "backend.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("proxy.py", "print(1)")
                zf.writestr("www/app.js", "console.log(1)")
            app_data = root / "AppData"
            expected_sha = launcher._sha256_file(archive)
            with mock.patch.object(launcher, "is_frozen", return_value=True), \
                    mock.patch.object(launcher, "bundle_root", return_value=meipass), \
                    mock.patch.object(launcher, "local_app_data_root", return_value=app_data):
                backend_dir = launcher.resolve_backend_dir()
            self.assertEqual(backend_dir, app_data / "backend" / expected_sha)
            self.assertEqual((backend_dir / "proxy.py").read_text(encoding="utf-8"), "print(1)")
            self.assertTrue((backend_dir / ".extracted").is_file())

    def test_frozen_mode_reuses_an_existing_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meipass = root / "_MEI"
            (meipass / "desktop").mkdir(parents=True)
            archive = meipass / "desktop" / "backend.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("proxy.py", "print(1)")
            app_data = root / "AppData"
            digest = launcher._sha256_file(archive)
            existing = app_data / "backend" / digest
            existing.mkdir(parents=True)
            (existing / ".extracted").write_text("ok", encoding="ascii")
            (existing / "proxy.py").write_text("cached", encoding="utf-8")
            with mock.patch.object(launcher, "is_frozen", return_value=True), \
                    mock.patch.object(launcher, "bundle_root", return_value=meipass), \
                    mock.patch.object(launcher, "local_app_data_root", return_value=app_data), \
                    mock.patch.object(launcher, "extract_backend_archive") as extract:
                backend_dir = launcher.resolve_backend_dir()
            extract.assert_not_called()
            self.assertEqual((backend_dir / "proxy.py").read_text(encoding="utf-8"), "cached")

    def test_frozen_mode_fails_loudly_when_the_zip_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            meipass = Path(tmp) / "_MEI"
            (meipass / "desktop").mkdir(parents=True)
            with mock.patch.object(launcher, "is_frozen", return_value=True), \
                    mock.patch.object(launcher, "bundle_root", return_value=meipass), \
                    mock.patch.object(launcher, "local_app_data_root",
                                      return_value=Path(tmp) / "AppData"):
                with self.assertRaisesRegex(launcher.BackendStartError, "missing"):
                    launcher.resolve_backend_dir()


class StdStreamTests(unittest.TestCase):
    """--windowed 打包后 sys.stdout/stderr 为 None，必须兜底。"""

    def test_none_stderr_becomes_a_writable_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_data = Path(tmp) / "AppData"
            original = sys.stderr
            try:
                sys.stderr = None
                with mock.patch.object(launcher, "local_app_data_root", return_value=app_data):
                    launcher.ensure_std_streams()
                self.assertIsNotNone(sys.stderr)
                sys.stderr.write("boom\n")
                sys.stderr.flush()
                log = app_data / "logs" / "desktop.log"
                self.assertTrue(log.is_file())
                self.assertIn("boom", log.read_text(encoding="utf-8"))
            finally:
                try:
                    if sys.stderr is not None and sys.stderr is not original:
                        sys.stderr.close()
                except OSError:
                    pass
                sys.stderr = original

    def test_real_streams_are_left_alone(self):
        original_out = sys.stdout
        original_err = sys.stderr
        launcher.ensure_std_streams()
        self.assertIs(sys.stdout, original_out)
        self.assertIs(sys.stderr, original_err)


if __name__ == "__main__":
    unittest.main()

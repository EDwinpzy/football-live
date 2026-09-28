import pathlib
import io
import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
LEAKED_SECRET = "cookie=abc123SECRET internal-host.internal:8094/metadata/1"


class StaticAssetCacheHeadersTest(unittest.TestCase):
    def test_real_get_uses_no_store_for_html_and_content_hash_cache_rules(self):
        with tempfile.TemporaryDirectory() as root:
            static_root = pathlib.Path(root)
            (static_root / "index.html").write_text(
                '<script src="/assets/app.abcdef0123456789.js"></script>',
                encoding="utf-8")
            (static_root / "app.js").write_text("window.app = true;", encoding="utf-8")
            (static_root / "assets").mkdir()
            (static_root / "assets" / "app.abcdef0123456789.js").write_text(
                "window.hashed = true;", encoding="utf-8")
            (static_root / "assets" / "site-deadbeef12345678.css").write_text(
                "body { color: black; }", encoding="utf-8")
            (static_root / "logo.png").write_bytes(b"png")
            (static_root / "style.css").write_text("body{}", encoding="utf-8")

            class StaticHandler(proxy.Handler):
                def __init__(self, *args, **kwargs):
                    SimpleHTTPRequestHandler.__init__(
                        self, *args, directory=root, **kwargs)

            server = ThreadingHTTPServer(("127.0.0.1", 0), StaticHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                expected = {
                    "/index.html": "no-store",
                    "/assets/app.abcdef0123456789.js":
                        "public, max-age=31536000, immutable",
                    "/assets/site-deadbeef12345678.css":
                        "public, max-age=31536000, immutable",
                    "/app.js": "public, max-age=300",
                # F-8: the front end versions assets with ?v=<n>, not path hashes.
                "/app.js?v=134": "public, max-age=31536000, immutable",
                "/style.css?v=131": "public, max-age=31536000, immutable",
                "/app.js?v=abcdef0123456789": "public, max-age=31536000, immutable",
                # Unversioned images keep the pre-existing 24h.
                "/logo.png": "public, max-age=86400",
                }
                for path, cache_control in expected.items():
                    with self.subTest(path=path):
                        connection.request("GET", path)
                        response = connection.getresponse()
                        body = response.read()
                        self.assertEqual(response.status, 200)
                        self.assertTrue(body)
                        self.assertEqual(response.getheader("Cache-Control"), cache_control)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)


class _ApiHandler(proxy.Handler):
    """Serve the /vod API surface without the static tree or real services."""

    def __init__(self, *args, **kwargs):
        SimpleHTTPRequestHandler.__init__(self, *args, directory=None, **kwargs)


class VodApiResponseSurfaceTest(unittest.TestCase):
    def _request(self, path):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8", "replace")
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_503_never_echoes_internal_exception_text_to_the_client(self):
        class FailingService:
            def resolve(self, _mytv_id, _episode):
                raise RuntimeError(LEAKED_SECRET)

        with patch.object(proxy, "_get_vod_service", return_value=FailingService()):
            status, body = self._request("/vod/resolve/10000123?episode=1")

        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"ok": False, "error": "vod unavailable"})
        self.assertNotIn("SECRET", body)
        self.assertNotIn("internal-host.internal", body)

    def test_resolve_access_log_drops_control_characters_from_the_method(self):
        class Handler(_ApiHandler):
            def do_GET(self):
                from urllib.parse import urlparse

                self.path = "/vod/resolve/10000123?episode=1"
                self.command = "GE\x1b]0;PWNED\x07T"
                self._vod_api(urlparse(self.path))

        class Service:
            def resolve(self, _mytv_id, _episode):
                return {"ok": True, "status": "MATCHED",
                        "preferred": None, "backups": []}

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        logged = io.StringIO()
        try:
            with patch.object(proxy, "_get_vod_service", return_value=Service()), \
                    patch("sys.stdout", logged):
                connection.request("GET", "/vod/resolve/10000123?episode=1")
                response = connection.getresponse()
                response.read()
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertEqual(response.status, 200)
        output = logged.getvalue()
        self.assertIn("/vod/resolve/:mytv_id", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\x07", output)
        self.assertNotIn("]0;", output)
        # Only letters survive, so the escape introducer cannot reach the log.
        self.assertRegex(output, r'"[A-Z]{1,12} /vod/resolve/:mytv_id"')


class SelfHostedComposeResolverTest(unittest.TestCase):
    """F-9: the self-hosted stack must wire the resolver to metadata-api."""

    def setUp(self):
        self.text = (REPO_ROOT / "deploy" / "selfhosted" / "compose.yaml").read_text(
            encoding="utf-8")

    def _web_api_block(self):
        lines = self.text.splitlines()
        start = next(index for index, line in enumerate(lines)
                     if line.strip() == "web-api:")
        end = len(lines)
        for index in range(start + 1, len(lines)):
            line = lines[index]
            # A sibling service starts at exactly two spaces of indentation.
            if (line.startswith("  ") and not line.startswith("   ")
                    and line.rstrip().endswith(":")):
                end = index
                break
        return "\n".join(lines[start:end])

    def test_web_api_receives_the_metadata_api_origin(self):
        self.assertIn("OTV_METADATA_API_URL: http://metadata-api:8094", self._web_api_block())

    def test_web_api_waits_for_metadata_api(self):
        block = self._web_api_block()
        self.assertIn("depends_on:", block)
        self.assertIn("metadata-api:", block.split("depends_on:", 1)[1])


if __name__ == "__main__":
    unittest.main()


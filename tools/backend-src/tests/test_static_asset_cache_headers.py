import pathlib
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()


from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


TOOLS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_ROOT))

import scf_main  # noqa: E402
import build_admin_function  # noqa: E402


class UpstreamState:
    requests: list[tuple[str, str, bytes, str | None]] = []
    response_status = 201
    response_body = b'{"ok":true}'


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self._respond()

    def do_POST(self):  # noqa: N802
        self._respond()

    def _respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        UpstreamState.requests.append(
            (self.command, self.path, body, self.headers.get("Cookie"))
        )
        self.send_response(UpstreamState.response_status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", "mytv-session=test; Path=/; HttpOnly; Secure")
        self.send_header("Content-Length", str(len(UpstreamState.response_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(UpstreamState.response_body)

    def log_message(self, fmt, *args):  # noqa: A003
        return


class CloudBaseAdminBridgeTests(unittest.TestCase):
    def setUp(self):
        UpstreamState.requests = []
        UpstreamState.response_status = 201
        UpstreamState.response_body = b'{"ok":true}'
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        upstream_url = f"http://127.0.0.1:{self.upstream.server_port}"
        factory = getattr(scf_main, "create_proxy_server", None)
        if not callable(factory):
            self.fail("CloudBase admin proxy server factory is missing")
        self.proxy = factory(upstream_url, max_response_bytes=1024)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        self.proxy_url = f"http://127.0.0.1:{self.proxy.server_port}"

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.proxy_thread.join(timeout=2)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=2)

    def test_legacy_admin_route_forwards_login_and_preserves_session_response(self):
        body = b'{"user":"admin","pass":"test-only"}'
        request = Request(
            self.proxy_url + "/admin/api/login?from=legacy",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Cookie": "old-session=abc"},
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 201)
            self.assertEqual(response.read(), UpstreamState.response_body)
            self.assertIn("mytv-session=test", response.headers.get("Set-Cookie", ""))
        self.assertEqual(
            UpstreamState.requests,
            [("POST", "/api/login?from=legacy", body, "old-session=abc")],
        )

    def test_current_admin_route_without_prefix_is_also_supported(self):
        with urlopen(self.proxy_url + "/api/whoami", timeout=5) as response:
            self.assertEqual(response.status, 201)
            response.read()
        self.assertEqual(UpstreamState.requests[0][1], "/api/whoami")

    def test_unlisted_and_traversal_paths_are_rejected_without_upstream_request(self):
        for path in ("/admin/../activate", "/admin/%2e%2e/activate", "/hotupdate/check"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(self.proxy_url + path, timeout=5)
                self.assertEqual(raised.exception.code, 404)
        self.assertEqual(UpstreamState.requests, [])

    def test_request_body_limit_is_checked_before_forwarding(self):
        self.proxy.max_request_bytes = 4
        request = Request(
            self.proxy_url + "/admin/api/login",
            data=b"12345",
            method="POST",
            headers={"Content-Type": "application/octet-stream"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 413)
        self.assertEqual(UpstreamState.requests, [])

    def test_response_body_limit_returns_gateway_error(self):
        UpstreamState.response_body = b"x" * 2048
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.proxy_url + "/admin/", timeout=5)
        self.assertEqual(raised.exception.code, 502)
        self.assertEqual(json.loads(raised.exception.read()), {"error": "upstream_response_too_large"})

    def test_production_origin_must_be_https_and_must_not_loop_to_cloudbase(self):
        for value in (
            "http://103.236.97.137:35208",
            "https://appletv-d5ge1bth794873f76.service.tcloudbase.com",
            "https://user:pass@example.com",
            "https://example.com/admin",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    scf_main.validate_upstream_origin(value)
        self.assertEqual(
            scf_main.validate_upstream_origin("https://103.236.97.137:35208/"),
            "https://103.236.97.137:35208",
        )

    def test_default_limit_allows_the_existing_four_mib_admin_upload_chunks(self):
        self.assertGreaterEqual(scf_main.MAX_REQUEST_BYTES, 4 * 1024 * 1024)

    def test_admin_function_package_includes_the_trust_ca(self):
        copy_ca = getattr(build_admin_function, "copy_bridge_ca", None)
        self.assertTrue(callable(copy_ca), "admin function builder does not copy the bridge CA")
        with __import__("tempfile").TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir)
            copy_ca(destination)
            packaged_ca = destination / "mytv_migration_ca.pem"
            source_ca = TOOLS_ROOT.parent / "cloudfunctions" / "activate" / "mytv_migration_ca.pem"
            self.assertEqual(packaged_ca.read_bytes(), source_ca.read_bytes())


if __name__ == "__main__":
    unittest.main()

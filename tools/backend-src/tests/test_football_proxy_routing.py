# -*- coding: utf-8 -*-
"""Android football requests use the bundled local proxy; normal relays stay direct."""
import http.client
import pathlib
import sys
import threading
import urllib.parse
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


PLAYLIST = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nchild.m3u8\n"


class _ResponseServer:
    def __init__(self, body, record_requests=False):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                if record_requests:
                    self.server.seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.seen = []
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FootballProxyRoutingTest(unittest.TestCase):
    def setUp(self):
        self.local_proxy = _ResponseServer(PLAYLIST, record_requests=True)
        self.direct_origin = _ResponseServer(PLAYLIST, record_requests=True)
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        self.backend_port = self.backend.server_address[1]
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.old_proxy_url = proxy.FOOTBALL_PROXY_URL
        self.old_blocked = proxy._blocked_internal_url
        proxy.FOOTBALL_PROXY_URL = self.local_proxy.url
        proxy._blocked_internal_url = lambda _url: False
        proxy.RELAY_CACHE.clear()
        proxy.MANIFEST_SEGS.clear()

    def tearDown(self):
        self.backend.shutdown()
        self.backend.server_close()
        self.local_proxy.close()
        self.direct_origin.close()
        proxy.FOOTBALL_PROXY_URL = self.old_proxy_url
        proxy._blocked_internal_url = self.old_blocked
        proxy.RELAY_CACHE.clear()
        proxy.MANIFEST_SEGS.clear()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.backend_port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        status = response.status
        conn.close()
        return status, body

    def test_only_marked_football_relay_uses_proxy_and_marks_child_playlist(self):
        football_url = "http://football-source.example/live/master.m3u8"
        football_path = "/api/relay?u=%s&football_vpn=1" % urllib.parse.quote(football_url, safe="")
        status, football_body = self._get(football_path)

        self.assertEqual(status, 200)
        self.assertIn("football_vpn=1", football_body)
        self.assertEqual(1, len(self.local_proxy.httpd.seen))
        self.assertTrue(self.local_proxy.httpd.seen[0].startswith("http://football-source.example/"))

        direct_url = self.direct_origin.url + "/live/master.m3u8"
        direct_path = "/api/relay?u=%s" % urllib.parse.quote(direct_url, safe="")
        status, direct_body = self._get(direct_path)

        self.assertEqual(status, 200)
        self.assertNotIn("football_vpn=1", direct_body)
        self.assertEqual(1, len(self.local_proxy.httpd.seen))
        self.assertEqual(1, len(self.direct_origin.httpd.seen))


if __name__ == "__main__":
    unittest.main()

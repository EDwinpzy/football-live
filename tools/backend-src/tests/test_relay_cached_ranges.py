"""Cached complete HLS segments retain correct HTTP Range semantics."""
import http.client
import http.server
import pathlib
import re
import sys
import threading
import time
import urllib.parse
import unittest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class _RangeOrigin:
    def __init__(self):
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                range_value = self.headers.get("Range")
                with owner.lock:
                    owner.requests.append((self.path, range_value))
                status, body, content_range = 200, owner.body, None
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_value or "", re.I)
                if match and (match.group(1) or match.group(2)):
                    if match.group(1):
                        start = int(match.group(1))
                        end = int(match.group(2)) if match.group(2) else len(owner.body) - 1
                    else:
                        suffix = int(match.group(2))
                        start = max(0, len(owner.body) - suffix)
                        end = len(owner.body) - 1
                    if end < start:
                        # Ignore malformed range syntax and return the full body.
                        pass
                    elif start >= len(owner.body):
                        status, body = 416, b""
                        content_range = "bytes */%d" % len(owner.body)
                    else:
                        end = min(end, len(owner.body) - 1)
                        status, body = 206, owner.body[start:end + 1]
                        content_range = "bytes %d-%d/%d" % (start, end, len(owner.body))

                self.send_response(status)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Accept-Ranges", "bytes")
                if content_range:
                    self.send_header("Content-Range", content_range)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.body = bytes(range(100))
        self.lock = threading.Lock()
        self.requests = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/live/segment.ts?token=test" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _RelayServer:
    def __init__(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RelayCachedRangesTests(unittest.TestCase):
    def setUp(self):
        self.origin = _RangeOrigin()
        self.relay = _RelayServer()
        self.old_blocked = proxy._blocked_internal_url
        self.old_cache = proxy.RELAY_CACHE.copy()
        self.old_manifest_segs = proxy.MANIFEST_SEGS.copy()
        proxy._blocked_internal_url = lambda _url: False
        proxy.RELAY_CACHE.clear()
        proxy.MANIFEST_SEGS.clear()
        self.path = "/api/relay?u=" + urllib.parse.quote(self.origin.url, safe="")

    def tearDown(self):
        self.relay.close()
        self.origin.close()
        proxy._blocked_internal_url = self.old_blocked
        proxy.RELAY_CACHE.clear()
        proxy.RELAY_CACHE.update(self.old_cache)
        proxy.MANIFEST_SEGS.clear()
        proxy.MANIFEST_SEGS.update(self.old_manifest_segs)

    def _cache_full_body(self):
        proxy.RELAY_CACHE[proxy._relay_cache_key(self.origin.url)] = (
            time.time(), 200, "video/mp2t", self.origin.body, False,
        )

    def _fetch(self, range_value=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.relay.port, timeout=5)
        headers = {"Range": range_value} if range_value is not None else {}
        conn.request("GET", self.path, headers=headers)
        response = conn.getresponse()
        body = response.read()
        result = (response.status, dict(response.getheaders()), body)
        conn.close()
        return result

    def test_cached_range_requests_return_exact_bytes_without_reopening_origin(self):
        self._cache_full_body()
        expected = self.origin.body[12:30]
        status, headers, body = self._fetch("bytes=12-29")
        self.assertEqual(status, 206)
        self.assertEqual(body, expected)
        self.assertEqual(headers["Content-Range"], "bytes 12-29/100")
        self.assertEqual(headers["Content-Length"], str(len(expected)))
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(self.origin.requests, [])

    def test_cached_suffix_and_no_range_requests_keep_expected_behavior(self):
        self._cache_full_body()
        status, headers, body = self._fetch("bytes=-7")
        self.assertEqual(status, 206)
        self.assertEqual(body, self.origin.body[-7:])
        self.assertEqual(headers["Content-Range"], "bytes 93-99/100")

        status, headers, body = self._fetch()
        self.assertEqual(status, 200)
        self.assertEqual(body, self.origin.body)
        self.assertEqual(headers["Content-Length"], "100")
        self.assertEqual(self.origin.requests, [])

    def test_unsatisfiable_cached_range_returns_416_without_upstream_request(self):
        self._cache_full_body()
        status, headers, body = self._fetch("bytes=100-")
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], "bytes */100")
        self.assertEqual(headers["Content-Length"], "0")
        self.assertEqual(body, b"")
        self.assertEqual(self.origin.requests, [])

    def test_empty_body_range_is_unsatisfiable_and_zero_suffix_falls_back(self):
        proxy.RELAY_CACHE[proxy._relay_cache_key(self.origin.url)] = (
            time.time(), 200, "video/mp2t", b"", False,
        )
        status, headers, body = self._fetch("bytes=-4")
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], "bytes */0")
        self.assertEqual(body, b"")
        self.assertEqual(self.origin.requests, [])

        self._cache_full_body()
        status, _headers, body = self._fetch("bytes=-0")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.origin.body)
        self.assertEqual(self.origin.requests, [("/live/segment.ts?token=test", "bytes=-0")])

    def test_non_200_or_manifest_cache_entries_are_not_used_for_ranges(self):
        key = proxy._relay_cache_key(self.origin.url)
        for entry in (
            (time.time(), 206, "video/mp2t", self.origin.body, False),
            (time.time(), 200, "application/vnd.apple.mpegurl", self.origin.body, True),
        ):
            with self.subTest(status=entry[1], is_manifest=entry[4]):
                proxy.RELAY_CACHE[key] = entry
                status, headers, body = self._fetch("bytes=0-70")
                self.assertEqual(status, 206)
                self.assertEqual(body, self.origin.body[:71])
                self.assertEqual(headers["Content-Range"], "bytes 0-70/100")
        self.assertEqual(
            self.origin.requests,
            [("/live/segment.ts?token=test", "bytes=0-70"),
             ("/live/segment.ts?token=test", "bytes=0-70")],
        )

    def test_cache_miss_forwards_range_to_origin(self):
        status, headers, body = self._fetch("bytes=20-87")
        self.assertEqual(status, 206)
        self.assertEqual(body, self.origin.body[20:88])
        self.assertEqual(headers["Content-Range"], "bytes 20-87/100")
        self.assertEqual(self.origin.requests, [("/live/segment.ts?token=test", "bytes=20-87")])
        self.assertNotIn(proxy._relay_cache_key(self.origin.url), proxy.RELAY_CACHE)

    def test_multi_and_invalid_ranges_fall_back_to_upstream(self):
        self._cache_full_body()
        for value in ("bytes=0-1,4-5", "items=0-1", "bytes=9-3"):
            with self.subTest(range=value):
                status, _headers, body = self._fetch(value)
                self.assertEqual(status, 200)
                self.assertEqual(body, self.origin.body)
        self.assertEqual(
            self.origin.requests,
            [("/live/segment.ts?token=test", "bytes=0-1,4-5"),
             ("/live/segment.ts?token=test", "items=0-1"),
             ("/live/segment.ts?token=test", "bytes=9-3")],
        )


if __name__ == "__main__":
    unittest.main()

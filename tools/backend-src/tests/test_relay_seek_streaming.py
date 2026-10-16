"""Seek and cancellation regressions for the shared media relay."""

import concurrent.futures
import http.server
import pathlib
import socket
import sys
import threading
import time
import unittest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402

_URLLIB3_AVAILABLE = proxy.HAS_URLLIB3


class _GatedUpstream:
    """A local HTTP source that sends small packets while its tail is gated."""

    def __init__(self, total=256 * 1024, first=4096, second=4096):
        self.total = total
        self.first = first
        self.second = second
        self.first_sent = threading.Event()
        self.allow_second = threading.Event()
        self.second_sent = threading.Event()
        self.allow_tail = threading.Event()
        self.client_closed = threading.Event()
        self.range_header = None
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _wait_or_closed(self, event):
                deadline = time.monotonic() + 2.0
                while not event.is_set() and time.monotonic() < deadline:
                    try:
                        self.connection.settimeout(0.025)
                        if self.connection.recv(1, socket.MSG_PEEK) == b"":
                            owner.client_closed.set()
                            self.close_connection = True
                            return False
                    except socket.timeout:
                        pass
                    except OSError:
                        owner.client_closed.set()
                        self.close_connection = True
                        return False
                return event.is_set()

            def do_GET(self):
                self.close_connection = True  # One scripted response per connection.
                owner.range_header = self.headers.get("Range")
                start, end, status = 0, owner.total - 1, 200
                if owner.range_header:
                    unit, value = owner.range_header.split("=", 1)
                    left, right = value.split("-", 1)
                    if unit != "bytes" or not left:
                        self.send_error(416)
                        return
                    start = int(left)
                    end = int(right) if right else owner.total - 1
                    status = 206
                end = min(end, owner.total - 1)
                length = max(0, end - start + 1)
                first_size = min(owner.first, length)
                second_size = min(owner.second, length - first_size)
                self.send_response(status)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{owner.total}")
                self.end_headers()
                self.wfile.write(b"a" * first_size)
                self.wfile.flush()
                owner.first_sent.set()

                if not self._wait_or_closed(owner.allow_second):
                    return
                try:
                    if second_size:
                        self.wfile.write(b"b" * second_size)
                        self.wfile.flush()
                    owner.second_sent.set()
                except OSError:
                    owner.client_closed.set()
                    self.close_connection = True
                    return

                if not self._wait_or_closed(owner.allow_tail):
                    return
                try:
                    tail_size = length - first_size - second_size
                    if tail_size:
                        self.wfile.write(b"c" * tail_size)
                        self.wfile.flush()
                except OSError:
                    owner.client_closed.set()
                    self.close_connection = True

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}/film.ts"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.allow_second.set()
        self.allow_tail.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RelaySeekStreamingTests(unittest.TestCase):
    def setUp(self):
        self._old_backend = proxy.HAS_URLLIB3
        self._old_blocked = proxy._blocked_internal_url
        proxy._blocked_internal_url = lambda _url: False
        self.upstreams = []

    def tearDown(self):
        proxy.HAS_URLLIB3 = self._old_backend
        proxy._blocked_internal_url = self._old_blocked
        for upstream in self.upstreams:
            upstream.close()

    def _upstream(self, **kwargs):
        upstream = _GatedUpstream(**kwargs)
        self.upstreams.append(upstream)
        return upstream

    @staticmethod
    def _backends():
        if _URLLIB3_AVAILABLE:
            return (("urllib3", True), ("stdlib", False))
        return (("stdlib", False),)

    def test_range_seek_returns_each_available_packet_without_filling_read_size(self):
        for backend_name, use_urllib3 in self._backends():
            with self.subTest(backend=backend_name):
                proxy.HAS_URLLIB3 = use_urllib3
                upstream = self._upstream()
                request_range = "bytes=131072-196607"
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    pending = pool.submit(
                        proxy.relay_stream, upstream.url, "none", request_range)
                    stream = None
                    second_pending = None
                    try:
                        self.assertTrue(upstream.first_sent.wait(2))
                        status, _ctype, extra, stream, _is_m3u8 = pending.result(timeout=1)
                        self.assertEqual(upstream.range_header, request_range)
                        self.assertEqual(status, 206)
                        self.assertEqual(extra.get("Content-Range"), "bytes 131072-196607/262144")
                        self.assertEqual(extra.get("Content-Length"), "65536")
                        self.assertEqual(len(next(stream)), upstream.first)

                        # A second small packet arrives while the source still holds
                        # the rest of the response. The relay's next read must pass
                        # it through without waiting to fill its 128 KiB block.
                        upstream.allow_second.set()
                        second_pending = pool.submit(next, stream)
                        self.assertTrue(upstream.second_sent.wait(2))
                        self.assertEqual(len(second_pending.result(timeout=1)), upstream.second)
                    finally:
                        upstream.allow_second.set()
                        upstream.allow_tail.set()
                        if stream is None:
                            try:
                                stream = pending.result(timeout=2)[3]
                            except Exception:
                                pass
                        if second_pending is not None and not second_pending.done():
                            try:
                                second_pending.result(timeout=2)
                            except Exception:
                                pass
                        if stream is not None:
                            stream.close()

    def test_cancel_before_first_media_chunk_closes_upstream(self):
        for backend_name, use_urllib3 in self._backends():
            with self.subTest(backend=backend_name):
                proxy.HAS_URLLIB3 = use_urllib3
                # The source provides one full initial read block so the old relay
                # also returns; the regression here is close-before-first-next().
                upstream = self._upstream(first=80 * 1024)
                status, _ctype, _extra, stream, _is_m3u8 = proxy.relay_stream(upstream.url)
                self.assertEqual(status, 200)
                cache_key = proxy._relay_cache_key(upstream.url)
                proxy.RELAY_CACHE.pop(cache_key, None)

                stream.close()
                self.assertTrue(upstream.client_closed.wait(1.5))
                self.assertNotIn(cache_key, proxy.RELAY_CACHE)

    def test_cancel_after_partial_chunk_closes_upstream_and_never_caches_partial_body(self):
        for backend_name, use_urllib3 in self._backends():
            with self.subTest(backend=backend_name):
                proxy.HAS_URLLIB3 = use_urllib3
                upstream = self._upstream(first=80 * 1024)
                status, _ctype, _extra, stream, _is_m3u8 = proxy.relay_stream(upstream.url)
                self.assertEqual(status, 200)
                cache_key = proxy._relay_cache_key(upstream.url)
                proxy.RELAY_CACHE.pop(cache_key, None)

                partial = next(stream)
                self.assertGreater(len(partial), 0)
                self.assertLess(len(partial), upstream.total)
                stream.close()
                self.assertTrue(upstream.client_closed.wait(1.5))
                self.assertNotIn(cache_key, proxy.RELAY_CACHE)


if __name__ == "__main__":
    unittest.main()

"""End-to-end local HTTP checks for HLS child-playlist prefetch behavior."""
import http.server
import pathlib
import sys
import threading
import urllib.parse
import unittest
from unittest import mock

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class _ImmediatePool:
    """Run submitted prefetch work inline while retaining real HTTP requests."""

    @staticmethod
    def submit(fn):
        fn()


class _FakeRelayResponse:
    def __init__(self, status, body):
        self.status = status
        self.data = body
        self.headers = {"Content-Type": "application/vnd.apple.mpegurl"}
        self.close_calls = 0
        self.release_calls = 0

    def read(self):
        return self.data

    def close(self):
        self.close_calls += 1

    def release_conn(self):
        self.release_calls += 1


class _HlsOrigin:
    def __init__(self):
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                with owner.lock:
                    owner.requests.append((self.path, self.headers.get("Referer")))
                if self.path == "/live/master.m3u8":
                    body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nchild.m3u8\n"
                    content_type = "application/vnd.apple.mpegurl"
                elif self.path == "/live/child.m3u8":
                    body = (
                        b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
                        b"#EXTINF:2,\nseg-1.ts\n#EXTINF:2,\nseg-2.ts\n"
                    )
                    content_type = "application/vnd.apple.mpegurl"
                elif self.path in ("/live/seg-1.ts", "/live/seg-2.ts"):
                    body = b"T" * 512
                    content_type = "video/mp2t"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.lock = threading.Lock()
        self.requests = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:%d/live/" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RelayPrefetchFlowTests(unittest.TestCase):
    def setUp(self):
        self.origin = _HlsOrigin()
        self.old_blocked = proxy._blocked_internal_url
        self.relay_cache = proxy.RELAY_CACHE.copy()
        self.manifest_segs = proxy.MANIFEST_SEGS.copy()
        self.prefetch_inflight = proxy._prefetch_inflight.copy()
        proxy._blocked_internal_url = lambda _url: False
        proxy.RELAY_CACHE.clear()
        proxy.MANIFEST_SEGS.clear()
        proxy._prefetch_inflight.clear()

    def tearDown(self):
        self.origin.close()
        proxy._blocked_internal_url = self.old_blocked
        proxy.RELAY_CACHE.clear()
        proxy.RELAY_CACHE.update(self.relay_cache)
        proxy.MANIFEST_SEGS.clear()
        proxy.MANIFEST_SEGS.update(self.manifest_segs)
        proxy._prefetch_inflight.clear()
        proxy._prefetch_inflight.update(self.prefetch_inflight)

    @staticmethod
    def _relay_target(line):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(line).query)
        return query["u"][0], query["ref"][0]

    def test_prefetch_uses_same_child_playlist_referer_as_client_segments(self):
        master_url = urllib.parse.urljoin(self.origin.base_url, "master.m3u8")
        child_url = urllib.parse.urljoin(self.origin.base_url, "child.m3u8")

        for backend_name, use_urllib3 in (("urllib3", True), ("stdlib", False)):
            with self.subTest(backend=backend_name):
                proxy.RELAY_CACHE.clear()
                with mock.patch.object(proxy, "HAS_URLLIB3", use_urllib3), \
                        mock.patch.object(proxy, "_prefetch_pool", _ImmediatePool()):
                    master = proxy.relay_url(master_url)
                    self.assertEqual(master[0], 200)
                    rewritten_child, child_ref = self._relay_target(
                        master[2].decode("utf-8").splitlines()[-1]
                    )
                    self.assertEqual(rewritten_child, child_url)
                    self.assertEqual(child_ref, master_url)

                    child = proxy.relay_url(rewritten_child, child_ref)
                    self.assertEqual(child[0], 200)
                    segment_url, segment_ref = self._relay_target(
                        child[2].decode("utf-8").splitlines()[3]
                    )
                    self.assertEqual(
                        segment_url, urllib.parse.urljoin(self.origin.base_url, "seg-1.ts"))
                    self.assertEqual(segment_ref, child_url)

                prefetched = [
                    referer for path, referer in self.origin.requests
                    if path in ("/live/seg-1.ts", "/live/seg-2.ts")
                ]
                self.assertEqual(prefetched[-2:], [child_url, child_url])

    def test_manifest_prewarms_live_edge_segments_newest_first(self):
        """Live players start at the newest edge, not the oldest playlist entry."""
        manifest_url = "https://origin.example/live/child.m3u8"
        raw_segments = [
            "https://origin.example/live/seg-1.ts?token=old",
            "https://origin.example/live/seg-2.ts?token=old",
            "https://origin.example/live/seg-3.ts?token=old",
            "https://origin.example/live/seg-4.ts?token=old",
            "https://origin.example/live/seg-5.ts?token=new",
            "https://origin.example/live/seg-6.ts?token=newest",
        ]
        calls = []

        with mock.patch.object(
            proxy,
            "_prefetch_segments",
            side_effect=lambda segments, ref: calls.append((list(segments), ref)),
        ):
            proxy._remember_manifest(manifest_url, raw_segments, manifest_url)

        self.assertEqual(calls, [(
            list(reversed(raw_segments[-4:])),
            manifest_url,
        )])

    def test_relay_url_releases_success_and_failed_strategy_responses(self):
        manifest = b"#EXTM3U\n#EXTINF:2,\nseg.ts\n"
        url = "https://source.example/live.m3u8"
        ref = "https://player.example/watch/"

        for backend_name, use_urllib3 in (("urllib3", True), ("stdlib", False)):
            with self.subTest(backend=backend_name):
                proxy.RELAY_CACHE.clear()
                failed = _FakeRelayResponse(403, b"denied")
                succeeded = _FakeRelayResponse(200, manifest)
                with mock.patch.object(proxy, "HAS_URLLIB3", use_urllib3), \
                        mock.patch.object(
                            proxy, "_open_upstream",
                            side_effect=[(failed, url), (succeeded, url)]), \
                        mock.patch.object(proxy, "_remember_manifest"):
                    result = proxy.relay_url(url, ref)

                self.assertEqual(result[0], 200)
                if use_urllib3:
                    self.assertEqual(failed.release_calls, 1)
                    self.assertEqual(succeeded.release_calls, 1)
                    self.assertEqual((failed.close_calls, succeeded.close_calls), (0, 0))
                else:
                    self.assertEqual((failed.close_calls, succeeded.close_calls), (1, 1))
                    self.assertEqual((failed.release_calls, succeeded.release_calls), (0, 0))


if __name__ == "__main__":
    unittest.main()

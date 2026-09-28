"""Line-probe concurrency must not be squeezed by the DNS lookup budget.

F-1 regression: ``/vod/detail`` probes up to ``PROBE_MAX_LINES`` (18) lines in
parallel and every probe resolves its host twice (URL pre-check + bounded
connect). The process-wide DNS pool used to hold only 8 slots and rejected the
overflow immediately, so ``probe_stream`` turned "no DNS slot" into
``alive=False``. With ordinary DNS latency that killed 10 of 18 healthy lines.

These tests drive ``registry.probe_stream`` for real -- the DNS path is never
stubbed out -- with a delayed ``getaddrinfo`` and a fake socket so every line
has to survive both lookups.
"""

import pathlib
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import source_dns  # noqa: E402
import vod_sources  # noqa: E402


PLAYLIST = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720\nseg.ts\n"
# The capacity regression only shows up once a lookup outlasts the time it takes
# to fill every slot. With 18 concurrent probes (36 lookups) against the pre-fix
# 8-slot pool, a 50ms lookup still fit; 300ms reproduces the reported "8 of 18
# lines dead" exactly, because the pool drains and the remaining probes are
# rejected before they ever start.
DNS_LATENCY_SECONDS = 0.3


class _FakeSocket:
    """Connected-socket stand-in: the probe only needs ``settimeout``/``close``."""

    def __init__(self, *args, **kwargs):
        pass

    def settimeout(self, _timeout):
        pass

    def connect(self, _address):
        pass

    def close(self):
        pass


class _PlaylistResponse:
    """Serves the probe body so no real socket or HTTP server is involved."""

    def __init__(self, body):
        self._body = body
        self.headers = None
        self.fp = None

    def geturl(self):
        return "https://cdn.example.test/1.m3u8"

    def read(self, _size=-1):
        body, self._body = self._body, b""
        return body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class ProbeDnsCapacityTest(unittest.TestCase):
    def _registry(self, line_count):
        source = vod_sources.VodSource(
            id="probe-src", name="probe-src",
            site_url="https://source.example.test",
            api_url="https://source.example.test/api.php/provide/vod/",
            adapter_type="macms_json", enabled=True, revision=1)
        return vod_sources.SourceRegistry([source], version=1)

    def _lines(self, line_count):
        return [{
            "id": "probe-src:content:%d" % index,
            "source_id": "probe-src",
            "source_name": "probe-src",
            "name": "line-%d" % index,
            "height": 0,
            "latency_ms": None,
            "alive": None,
            "episodes": [{
                "index": 0,
                "name": "第1集",
                "url": "https://cdn%d.example.test/%d.m3u8" % (index, index),
            }],
        } for index in range(line_count)]

    def _delayed_getaddrinfo(self, calls):
        def resolve(_host, port, *args, **kwargs):
            calls.append(1)
            time.sleep(DNS_LATENCY_SECONDS)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     "", ("93.184.216.34", port))]
        return resolve

    def test_slow_dns_keeps_every_probed_line_alive(self):
        line_count = vod_sources.PROBE_MAX_LINES
        self.assertGreaterEqual(line_count, 18)
        registry = self._registry(line_count)
        lines = self._lines(line_count)
        calls = []
        lock = threading.Lock()

        def counted_getaddrinfo(host, port, *args, **kwargs):
            with lock:
                calls.append(1)
            time.sleep(DNS_LATENCY_SECONDS)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     "", ("93.184.216.34", port))]

        class FakeOpener:
            def open(self, request, timeout=None):
                return _PlaylistResponse(PLAYLIST)

        started = time.monotonic()
        try:
            with patch.object(source_dns.socket, "socket", return_value=_FakeSocket()), \
                    patch("socket.getaddrinfo", side_effect=counted_getaddrinfo), \
                    patch.object(vod_sources, "_SOURCE_URL_OPENER", FakeOpener()):
                # Real fan-out: 18 lines probed at once through the shared pool.
                pending = registry._race_probe(lines, started + vod_sources.PROBE_TIMEOUT_SECONDS)
                for future in pending:
                    future.result(timeout=2)
        finally:
            registry.close()
        elapsed = time.monotonic() - started

        alive = [line for line in lines if line.get("alive")]
        self.assertEqual(len(alive), line_count,
                         "DNS capacity pressure must not be reported as dead lines")
        self.assertLessEqual(elapsed, vod_sources.PROBE_TIMEOUT_SECONDS + 1.0)
        # Every line resolves twice (pre-check + bounded connect).
        self.assertGreaterEqual(len(calls), line_count * 2)

    def test_capacity_rejection_never_counts_as_a_dead_line(self):
        registry = self._registry(1)
        line = self._lines(1)[0]
        exhausted = source_dns.BoundedDNSResolver(max_workers=1, timeout_seconds=0.05)
        release = threading.Event()
        started = threading.Event()

        def stalled(host, port, *args, **kwargs):
            started.set()
            release.wait(2)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     "", ("93.184.216.34", port))]

        previous = source_dns._resolver
        source_dns._resolver = exhausted
        self.addCleanup(setattr, source_dns, "_resolver", previous)

        def occupy():
            try:
                source_dns.getaddrinfo("busy.example.test", 443, timeout=0.05)
            except TimeoutError:
                pass

        try:
            with patch("socket.getaddrinfo", side_effect=stalled):
                thread = threading.Thread(target=occupy, daemon=True)
                thread.start()
                self.assertTrue(started.wait(1))
                with self.assertRaises(source_dns.DNSCapacityError):
                    source_dns.getaddrinfo("busy.example.test", 443, timeout=0.05)
                result = vod_sources.probe_stream(
                    "https://busy.example.test/1.m3u8")
        finally:
            release.set()
            thread.join(timeout=2)

        self.assertNotEqual(result.get("alive"), False,
                            "DNS capacity exhaustion must stay unknown, not dead")


if __name__ == "__main__":
    unittest.main()

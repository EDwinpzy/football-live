import pathlib
import socket
import sys
import threading
import time
import unittest
import concurrent.futures
from unittest.mock import patch


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import source_dns  # noqa: E402


class BoundedDNSResolverTest(unittest.TestCase):
    def test_connection_blocks_rebinding_to_loopback_before_connect(self):
        host = "rebinding.test"
        answers = ["8.8.8.8", "127.0.0.1"]
        lookup_count = [0]

        def resolve(hostname, port, *args, **kwargs):
            self.assertEqual(hostname, host)
            address = answers[lookup_count[0]]
            lookup_count[0] += 1
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     "", (address, port))]

        class FakeSocket:
            def __init__(self):
                self.connect_calls = []

            def settimeout(self, _timeout):
                pass

            def bind(self, _address):
                pass

            def connect(self, address):
                self.connect_calls.append(address)

            def close(self):
                pass

        fake_socket = FakeSocket()
        connection = None
        connection_error = None
        with patch("socket.getaddrinfo", side_effect=resolve), \
                patch("source_dns.socket.socket", return_value=fake_socket):
            validated_addresses = source_dns.getaddrinfo(host, 443, timeout=1)
            self.assertEqual(validated_addresses[0][4][0], "8.8.8.8")
            try:
                connection = source_dns._bounded_create_connection(
                    (host, 443), timeout=1)
            except (OSError, ValueError) as exc:
                connection_error = exc

        if connection is not None:
            connection.close()
        self.assertEqual(lookup_count[0], 2)
        self.assertEqual(fake_socket.connect_calls, [])
        self.assertIsNotNone(connection_error)

    def test_stalled_lookups_are_bounded_and_capacity_recovers(self):
        resolver = source_dns.BoundedDNSResolver(max_workers=2, timeout_seconds=0.04)
        release = threading.Event()
        started = threading.Event()
        calls = [0]
        calls_lock = threading.Lock()

        def stalled_getaddrinfo(host, port, *args, **kwargs):
            with calls_lock:
                calls[0] += 1
                if calls[0] == resolver.max_workers:
                    started.set()
            release.wait()
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     "", ("8.8.8.8", port))]

        with patch("socket.getaddrinfo", side_effect=stalled_getaddrinfo):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as callers:
                pending = [callers.submit(
                    resolver.getaddrinfo, "source.test", 443, timeout=0.04)
                    for _ in range(resolver.max_workers)]
                self.assertTrue(started.wait(1))
                for future in pending:
                    with self.assertRaises(TimeoutError):
                        future.result(timeout=0.5)

                with self.assertRaisesRegex(TimeoutError, "capacity"):
                    resolver.getaddrinfo("source.test", 443, timeout=0.04)
                self.assertEqual(calls[0], resolver.max_workers)

                release.set()
                deadline = time.monotonic() + 1
                recovered = False
                while time.monotonic() < deadline:
                    try:
                        resolver.getaddrinfo("source.test", 443, timeout=0.2)
                        recovered = True
                        break
                    except TimeoutError as exc:
                        if "capacity" not in str(exc):
                            raise
                        time.sleep(0.01)
                self.assertTrue(recovered)
                self.assertEqual(calls[0], resolver.max_workers + 1)


if __name__ == "__main__":
    unittest.main()

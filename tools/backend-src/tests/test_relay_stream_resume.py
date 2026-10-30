"""Range-resume regressions for short full-GET media responses."""

import http.client
import pathlib
import sys
import unittest
from unittest import mock

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class _FakeResponse:
    def __init__(self, status, headers, parts):
        self.status = status
        self.headers = dict(headers)
        self.parts = list(parts)
        self.close_calls = 0
        self.release_calls = 0

    def read1(self, size):
        if not self.parts:
            return b""
        item = self.parts.pop(0)
        if isinstance(item, BaseException):
            raise item
        if len(item) > size:
            self.parts.insert(0, item[size:])
            return item[:size]
        return item

    def read(self, size):
        return self.read1(size)

    def close(self):
        self.close_calls += 1

    def release_conn(self):
        self.release_calls += 1


class RelayStreamResumeTests(unittest.TestCase):
    def setUp(self):
        self.old_blocked = proxy._blocked_internal_url
        self.old_urllib3 = proxy.HAS_URLLIB3
        self.old_cache = proxy.RELAY_CACHE.copy()
        proxy._blocked_internal_url = lambda _url: False
        proxy.HAS_URLLIB3 = False
        proxy.RELAY_CACHE.clear()
        self.url = "https://source.example/live/a1.ts"

    def tearDown(self):
        proxy._blocked_internal_url = self.old_blocked
        proxy.HAS_URLLIB3 = self.old_urllib3
        proxy.RELAY_CACHE.clear()
        proxy.RELAY_CACHE.update(self.old_cache)

    @staticmethod
    def _full_headers(total, etag='"a1-strong"', accept_ranges=True):
        headers = {
            "Content-Type": "video/mp2t",
            "Content-Length": str(total),
            "ETag": etag,
        }
        if accept_ranges:
            headers["Accept-Ranges"] = "bytes"
        return headers

    @staticmethod
    def _range_headers(start, end, total, etag='"a1-strong"'):
        return {
            "Content-Type": "video/mp2t",
            "Content-Length": str(end - start + 1),
            "Content-Range": f"bytes {start}-{end}/{total}",
            "ETag": etag,
        }

    def _run(self, responses):
        calls = []
        timeouts = []

        def open_upstream(url, headers, **_kwargs):
            calls.append(dict(headers))
            timeouts.append(_kwargs.get("read_timeout"))
            return responses.pop(0), url

        chunks = []
        error = None
        with mock.patch.object(proxy, "_open_upstream", side_effect=open_upstream):
            status, _ctype, extra, stream, _is_m3u8 = proxy.relay_stream(
                self.url, "none")
            try:
                for chunk in stream:
                    chunks.append(chunk)
            except Exception as exc:  # Assert the caller still sees a network-style failure.
                error = exc
            finally:
                stream.close()
        return status, extra, b"".join(chunks), error, calls, timeouts

    def test_resume_recovers_exact_bytes_and_counts_incomplete_read_partial(self):
        body = bytes(i % 251 for i in range(512 * 1024))
        first = body[:64 * 1024]
        second = body[64 * 1024:80 * 1024]
        partial = body[80 * 1024:80 * 1024 + 13]
        resume_at = len(first) + len(second) + len(partial)
        initial = _FakeResponse(
            200,
            self._full_headers(len(body)),
            [first, second, http.client.IncompleteRead(partial, len(body) - 80 * 1024)],
        )
        resumed = _FakeResponse(
            206,
            self._range_headers(resume_at, len(body) - 1, len(body)),
            [body[resume_at:]],
        )

        _status, extra, data, error, calls, timeouts = self._run([initial, resumed])

        self.assertIsNone(error)
        self.assertEqual(data, body)
        self.assertEqual(extra["Content-Length"], str(len(body)))
        self.assertEqual(calls[1]["Range"], f"bytes={resume_at}-")
        self.assertEqual(calls[1]["If-Range"], '"a1-strong"')
        self.assertEqual(timeouts[1], 5)
        self.assertEqual((initial.close_calls, resumed.close_calls), (1, 1))
        self.assertEqual(proxy.RELAY_CACHE[proxy._relay_cache_key(self.url)][3], body)

    def test_resume_rejects_range_ignored_200_without_appending_body(self):
        body = b"z" * (256 * 1024)
        first = body[:64 * 1024]
        initial = _FakeResponse(200, self._full_headers(len(body)), [first, b""])
        ignored = _FakeResponse(200, self._full_headers(len(body)), [body])

        _status, _extra, data, error, calls, _timeouts = self._run([initial, ignored])

        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(data, first)
        self.assertEqual(calls[1]["Range"], f"bytes={len(first)}-")
        self.assertEqual((initial.close_calls, ignored.close_calls), (1, 1))
        self.assertNotIn(proxy._relay_cache_key(self.url), proxy.RELAY_CACHE)

    def test_resume_rejects_changed_etag(self):
        body = b"e" * (256 * 1024)
        first = body[:64 * 1024]
        initial = _FakeResponse(200, self._full_headers(len(body)), [first, b""])
        changed = _FakeResponse(
            206,
            self._range_headers(len(first), len(body) - 1, len(body), etag='"other"'),
            [body[len(first):]],
        )

        _status, _extra, data, error, _calls, _timeouts = self._run([initial, changed])

        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(data, first)
        self.assertNotIn(proxy._relay_cache_key(self.url), proxy.RELAY_CACHE)

    def test_resume_stops_after_two_attempts(self):
        total = 300 * 1024
        first = b"a" * (64 * 1024)
        resume_one = b"b" * (20 * 1024)
        resume_two = b"c" * (20 * 1024)
        start_one = len(first)
        start_two = start_one + len(resume_one)
        initial = _FakeResponse(200, self._full_headers(total), [first, b""])
        first_retry = _FakeResponse(
            206, self._range_headers(start_one, total - 1, total), [resume_one, b""])
        second_retry = _FakeResponse(
            206, self._range_headers(start_two, total - 1, total), [resume_two, b""])

        _status, _extra, data, error, calls, _timeouts = self._run(
            [initial, first_retry, second_retry])

        self.assertIsNotNone(error)
        self.assertEqual(data, first + resume_one + resume_two)
        self.assertEqual(len(calls), 3)  # initial GET + at most two Range resumes
        self.assertEqual(calls[1]["Range"], f"bytes={start_one}-")
        self.assertEqual(calls[2]["Range"], f"bytes={start_two}-")

    def test_no_range_support_keeps_short_body_failure(self):
        total = 128 * 1024
        first = b"r" * (64 * 1024)
        initial = _FakeResponse(
            200, self._full_headers(total, accept_ranges=False), [first, b""])

        _status, _extra, data, error, calls, _timeouts = self._run([initial])

        self.assertIsInstance(error, http.client.IncompleteRead)
        self.assertEqual(data, first)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(proxy._relay_cache_key(self.url), proxy.RELAY_CACHE)


if __name__ == "__main__":
    unittest.main()

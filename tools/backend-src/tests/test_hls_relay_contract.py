"""Football HLS relay regressions for signed redirects and encrypted streams."""

import pathlib
import re
import sys
import unittest
import urllib.parse
from unittest import mock


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class _FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self.data = body
        self._body = body

    def read(self, size=-1, **_kwargs):
        if size < 0:
            size = len(self._body)
        body, self._body = self._body[:size], self._body[size:]
        return body

    def release_conn(self):
        pass

    def close(self):
        pass


@unittest.skipUnless(proxy.HAS_URLLIB3, "redirect path requires urllib3")
class OpenUpstreamRedirectTests(unittest.TestCase):
    def test_relative_redirect_preserves_signed_query_and_returns_final_url(self):
        start = "http://origin.example/hls/master.m3u8?old=1"
        redirected = "http://origin.example/signed/master.m3u8?tm=ab%2Bcd&key=x"
        redirect = _FakeResponse(302, headers={"Location": "../signed/master.m3u8?tm=ab%2Bcd&key=x"})
        final = _FakeResponse(200, b"#EXTM3U")

        with mock.patch.object(proxy._http, "request", side_effect=[redirect, final]) as request:
            response, final_url = proxy._open_upstream(
                start, {"User-Agent": "test"}, read_timeout=1, preload=True)

        self.assertIs(response, final)
        self.assertEqual(final_url, redirected)
        self.assertEqual(request.call_args_list[1].args[1], redirected)

    def test_redirect_to_loopback_is_rejected_before_following(self):
        redirect = _FakeResponse(302, headers={"Location": "http://127.0.0.1/admin"})
        with mock.patch.object(proxy._http, "request", return_value=redirect) as request:
            with self.assertRaisesRegex(RuntimeError, "blocked redirect chain"):
                proxy._open_upstream(
                    "https://origin.example/master.m3u8", {}, read_timeout=1)
        self.assertEqual(request.call_count, 1)


class EncryptedHlsRelayTests(unittest.TestCase):
    def setUp(self):
        self._cache = proxy.RELAY_CACHE.copy()
        proxy.RELAY_CACHE.clear()

    def tearDown(self):
        proxy.RELAY_CACHE.clear()
        proxy.RELAY_CACHE.update(self._cache)

    def test_aes_key_and_segments_use_the_redirected_manifest_base(self):
        manifest_url = "https://origin.example/hls/master.m3u8?manifest=old"
        final_url = "https://edge.example/signed/live/index.m3u8?manifest=new"
        manifest = (
            '#EXTM3U\n'
            '#EXT-X-VERSION:3\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="../keys/key.bin?token=a+b",IV=0x1234\n'
            '#EXTINF:6.0,\n'
            'segments/001.ts?token=c+d\n'
        ).encode("utf-8")
        response = _FakeResponse(
            200, manifest, {"Content-Type": "application/vnd.apple.mpegurl"})
        prefetched = []

        with mock.patch.object(proxy, "_open_upstream", return_value=(response, final_url)), \
                mock.patch.object(proxy, "_remember_manifest",
                                  side_effect=lambda _url, segments, _ref: prefetched.extend(segments)):
            status, _content_type, body, error, is_m3u8 = proxy.relay_url(manifest_url, "none")

        self.assertEqual(status, 200)
        self.assertIsNone(error)
        self.assertTrue(is_m3u8)
        lines = body.decode("utf-8").splitlines()
        key_line = next(line for line in lines if line.startswith("#EXT-X-KEY:"))
        key_proxy_url = re.search(r'URI="([^"]+)"', key_line).group(1)
        key_target = urllib.parse.parse_qs(
            urllib.parse.urlsplit(key_proxy_url).query)["u"][0]
        self.assertEqual(key_target, "https://edge.example/signed/keys/key.bin?token=a+b")
        segment_proxy_url = next(line for line in lines if line and not line.startswith("#"))
        segment_target = urllib.parse.parse_qs(
            urllib.parse.urlsplit(segment_proxy_url).query)["u"][0]
        self.assertEqual(
            segment_target,
            "https://edge.example/signed/live/segments/001.ts?token=c+d")
        self.assertEqual(prefetched, [segment_target])


if __name__ == "__main__":
    unittest.main()

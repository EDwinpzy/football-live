# -*- coding: utf-8 -*-
"""Unicode URL normalization contract for playback relay requests."""
import pathlib
import sys
import unittest
from unittest import mock

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402
import vod_sources  # noqa: E402


class _Response:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}
        self.released = False

    def release_conn(self):
        self.released = True


class RelayUnicodeUrlTests(unittest.TestCase):
    def test_initial_url_uses_same_normalization_as_source_probe(self):
        raw_url = "https://example.test/影视/红楼梦.m3u8?签名=中文%2F保留#播放器片段"
        response = _Response(200)
        client = mock.Mock()
        client.request.return_value = response

        with mock.patch.object(proxy, "HAS_URLLIB3", True), \
                mock.patch.object(proxy, "_http_client_for", return_value=client):
            result, final_url = proxy._open_upstream(raw_url, {}, read_timeout=2)

        expected = vod_sources._ascii_http_url(raw_url)
        self.assertIs(result, response)
        self.assertEqual(client.request.call_args.args[:2], ("GET", expected))
        self.assertEqual(final_url, expected)
        self.assertIn("/%E5%BD%B1%E8%A7%86/", expected)
        self.assertIn("%E7%AD%BE%E5%90%8D=%E4%B8%AD%E6%96%87%2F%E4%BF%9D%E7%95%99", expected)
        self.assertNotIn("#", expected)

    def test_unicode_relative_redirect_is_normalized_before_next_request(self):
        raw_url = "https://example.test/start/list.m3u8?token=原始值"
        location = "/直播/比赛.m3u8?auth=签名%2Fkeep"
        redirect = _Response(302, {"Location": location})
        final = _Response(200)
        client = mock.Mock()
        client.request.side_effect = [redirect, final]

        with mock.patch.object(proxy, "HAS_URLLIB3", True), \
                mock.patch.object(proxy, "_http_client_for", return_value=client), \
                mock.patch.object(proxy, "_blocked_internal_url", return_value=False):
            result, final_url = proxy._open_upstream(raw_url, {}, read_timeout=2)

        first_url = vod_sources._ascii_http_url(raw_url)
        expected_final = vod_sources._ascii_http_url(
            proxy.urljoin(first_url, location))
        requested_urls = [call.args[1] for call in client.request.call_args_list]
        self.assertEqual(requested_urls, [first_url, expected_final])
        self.assertTrue(redirect.released)
        self.assertIs(result, final)
        self.assertEqual(final_url, expected_final)
        self.assertIn("%E7%9B%B4%E6%92%AD/%E6%AF%94%E8%B5%9B.m3u8", expected_final)

    def test_urllib_fallback_receives_normalized_url(self):
        raw_url = "https://example.test/剧集/第一集.m3u8?token=值"
        expected = vod_sources._ascii_http_url(raw_url)
        response = _Response(200)
        opener = mock.Mock()
        opener.open.return_value = response

        with mock.patch.object(proxy, "HAS_URLLIB3", False), \
                mock.patch.object(proxy, "_active_upstream_proxy", return_value=None), \
                mock.patch.object(proxy, "_no_redirect_opener", opener):
            result, final_url = proxy._open_upstream(raw_url, {}, read_timeout=2)

        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, expected)
        self.assertIs(result, response)
        self.assertEqual(final_url, expected)

    def test_urllib_fallback_normalizes_unicode_redirect(self):
        raw_url = "https://example.test/start/index.m3u8"
        location = "../直播/正片.m3u8?token=中文%2Fkeep"
        redirect = proxy.urlreq.HTTPError(
            raw_url, 302, "Found", {"Location": location}, None)
        response = _Response(200)
        opener = mock.Mock()
        opener.open.side_effect = [redirect, response]

        with mock.patch.object(proxy, "HAS_URLLIB3", False), \
                mock.patch.object(proxy, "_active_upstream_proxy", return_value=None), \
                mock.patch.object(proxy, "_no_redirect_opener", opener), \
                mock.patch.object(proxy, "_blocked_internal_url", return_value=False):
            result, final_url = proxy._open_upstream(raw_url, {}, read_timeout=2)

        expected = vod_sources._ascii_http_url(proxy.urljoin(raw_url, location))
        requests = [call.args[0].full_url for call in opener.open.call_args_list]
        self.assertEqual(requests, [vod_sources._ascii_http_url(raw_url), expected])
        self.assertIs(result, response)
        self.assertEqual(final_url, expected)


if __name__ == "__main__":
    unittest.main()

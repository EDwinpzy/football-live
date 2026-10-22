"""Public web/backend surface is limited to football live playback."""
import io
import json
import pathlib
import sys
import unittest
from unittest import mock
from urllib.parse import urlsplit


BACKEND = pathlib.Path(__file__).resolve().parents[1]
WWW = BACKEND / "www"
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


class FootballOnlySurfaceTest(unittest.TestCase):
    def dispatch_get(self, path):
        handler = object.__new__(proxy.Handler)
        handler.path = path
        handler.wfile = io.BytesIO()
        handler._status = None
        handler._headers = {}
        handler.send_response = lambda status, *_args: setattr(handler, "_status", status)
        handler.send_header = lambda key, value: handler._headers.__setitem__(key, value)
        handler.end_headers = lambda: None
        proxy.Handler._do_get(handler, urlsplit(path))
        return handler._status, handler._headers, handler.wfile.getvalue()

    def test_web_branding_and_product_controls_are_football_only(self):
        index = (WWW / "index.html").read_text(encoding="utf-8")
        app = (WWW / "app.js").read_text(encoding="utf-8")
        manifest = json.loads((WWW / "manifest.webmanifest").read_text(encoding="utf-8"))

        self.assertIn("<title>足球直播</title>", index)
        self.assertIn("<strong>足球直播</strong>", index)
        self.assertEqual("足球直播", manifest["name"])
        self.assertEqual("足球直播", manifest["short_name"])
        for retired in ("/api/activate", "/api/football-news", "/api/football-results",
                        "/api/football-videos", "/api/iptv", "/api/search", "/vod/", "/hhkan/"):
            self.assertNotIn(retired, app)
        for retired_label in ("会员", "卡密", "影视", "点播", "电视频道"):
            self.assertNotIn(retired_label, index)

    def test_retired_public_media_apis_return_not_found(self):
        for path in ("/api/activate", "/api/football-news", "/api/football-results",
                     "/api/football-videos", "/api/iptv", "/api/search", "/vod/home",
                     "/hhkan/home"):
            with self.subTest(path=path):
                status, _headers, body = self.dispatch_get(path)
                self.assertEqual(404, status)
                self.assertEqual({"ok": False, "error": "not found"}, json.loads(body))

    def test_match_listing_and_health_remain_public(self):
        matches = {"matches": [{"match_id": "42", "home": "甲队", "away": "乙队"}]}
        with mock.patch.object(proxy, "build_api", return_value=(matches, False)):
            status, _headers, body = self.dispatch_get("/api/matches?fresh=1")
        self.assertEqual(200, status)
        self.assertEqual(matches, json.loads(body))

        with mock.patch.object(proxy, "_web_asset_health", return_value={"ok": True}):
            status, _headers, body = self.dispatch_get("/api/health")
        self.assertEqual(200, status)
        health = json.loads(body)
        self.assertTrue(health["ok"])
        self.assertNotIn("vod", health)

    def test_stream_resolution_and_ts_relay_remain_public(self):
        resolved = {"sources": [{"id": "a2", "url": "https://example.test/live.m3u8"}]}
        with mock.patch.object(proxy, "resolve_88zhibo_streams", return_value=(resolved, False)) as resolve:
            status, _headers, body = self.dispatch_get("/api/stream/42?src=all")
        self.assertEqual(200, status)
        self.assertEqual(resolved, json.loads(body))
        resolve.assert_called_once_with("42", fresh=False)

        handler = object.__new__(proxy.Handler)
        handler.path = "/api/relayts/42?src=a2"
        handler.wfile = io.BytesIO()
        handler._status = None
        handler._headers = {}
        handler.send_response = lambda status, *_args: setattr(handler, "_status", status)
        handler.send_header = lambda key, value: handler._headers.__setitem__(key, value)
        handler.end_headers = lambda: None
        with mock.patch.object(proxy, "relayts_generator", return_value=iter([b"ts-data"])) as relay:
            proxy.Handler._do_get(handler, urlsplit(handler.path))
        self.assertEqual(200, handler._status)
        self.assertEqual("video/mp2t", handler._headers["Content-Type"])
        self.assertEqual(b"ts-data", handler.wfile.getvalue())
        relay.assert_called_once_with("42", "a2")

    def test_public_activation_post_is_disabled(self):
        handler = object.__new__(proxy.Handler)
        handler.path = "/api/activate"
        handler.wfile = io.BytesIO()
        handler._status = None
        handler._headers = {}
        handler.send_response = lambda status, *_args: setattr(handler, "_status", status)
        handler.send_header = lambda key, value: handler._headers.__setitem__(key, value)
        handler.end_headers = lambda: None
        proxy.Handler.do_POST(handler)
        self.assertEqual(404, handler._status)
        self.assertEqual({"ok": False, "error": "not found"}, json.loads(handler.wfile.getvalue()))

    def test_7m_score_fetcher_keeps_host_allowlist_and_refuses_redirects(self):
        class Response:
            def __init__(self, status, body=b"score"):
                self.status = status
                self.body = body

            def read(self):
                return self.body

        connection = mock.Mock()
        connection.getresponse.return_value = Response(200, "比分".encode("gb18030"))
        with mock.patch("http.client.HTTPConnection", return_value=connection) as create:
            body = proxy._fetch_7m_text("http://js-live.7m.com.cn/live.js?day=today")
        self.assertEqual("比分", body)
        create.assert_called_once_with("js-live.7m.com.cn", 80, timeout=12)
        connection.request.assert_called_once_with(
            "GET", "/live.js?day=today",
            headers={"User-Agent": proxy.UA, "Host": "js-live.7m.com.cn"})
        connection.close.assert_called_once()

        for url in ("https://evil.example/live.js", "http://js-live.7m.com.cn.evil/live.js",
                    "ftp://js-live.7m.com.cn/live.js"):
            with self.subTest(url=url), mock.patch("http.client.HTTPConnection") as create:
                with self.assertRaisesRegex(RuntimeError, "host not allowed"):
                    proxy._fetch_7m_text(url)
                create.assert_not_called()

        connection = mock.Mock()
        connection.getresponse.return_value = Response(302)
        with mock.patch("http.client.HTTPConnection", return_value=connection) as create:
            with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                proxy._fetch_7m_text("http://js-live.7m.com.cn/live.js")
        create.assert_called_once()
        connection.request.assert_called_once()
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

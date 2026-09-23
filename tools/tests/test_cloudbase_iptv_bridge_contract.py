import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("cloudbase_iptv", ROOT / "cloudfunctions" / "iptv" / "index.py")
IPTV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IPTV)


class _FakeResponse:
    status = 206
    headers = {
        "Content-Type": "audio/x-mpegurl; charset=utf-8",
        "Cache-Control": "no-cache",
        "Content-Range": "bytes 0-8/9",
    }

    def read(self, _limit):
        return b"#EXTM3U\n"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeOpener:
    def __init__(self):
        self.request = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return _FakeResponse()


class CloudBaseIptvBridgeTests(unittest.TestCase):
    def test_timer_is_retired_without_writing_cloudbase(self):
        with patch.object(IPTV, "rebuild", side_effect=AssertionError("must not run"), create=True):
            result = IPTV.main_handler({"Type": "Timer"}, None)
        self.assertTrue(result["retired"])
        self.assertEqual(result["ret"], 0)

    def test_latest_proxies_only_playlist_route_and_preserves_range_and_status(self):
        opener = _FakeOpener()
        with patch.object(IPTV, "SELFHOSTED_BASE", "https://tv.example:35208"), \
             patch.object(IPTV.urlreq, "build_opener", return_value=opener), \
             patch.object(IPTV.ssl, "create_default_context"):
            result = IPTV.main_handler({
                "httpMethod": "GET",
                "path": "/latest?lang=zh",
                "headers": {"Range": "bytes=0-8", "Cookie": "must-not-forward"},
            }, None)

        self.assertEqual(result["statusCode"], 206)
        self.assertTrue(result["body"].startswith("#EXTM3U"))
        self.assertEqual(result["headers"]["Content-Range"], "bytes 0-8/9")
        self.assertEqual(opener.request.full_url, "https://tv.example:35208/iptv/latest?lang=zh")
        self.assertEqual(opener.request.get_header("Range"), "bytes=0-8")
        self.assertIsNone(opener.request.get_header("Cookie"))

    def test_unlisted_iptv_paths_are_not_proxied(self):
        with patch.object(IPTV.urlreq, "build_opener", side_effect=AssertionError("must not proxy")):
            result = IPTV.main_handler({"httpMethod": "POST", "path": "/refresh"}, None)
        self.assertEqual(result["statusCode"], 404)


if __name__ == "__main__":
    unittest.main()

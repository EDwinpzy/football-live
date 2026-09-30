import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


class ImeHotwordsHttpTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/api/ime/hotwords" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_hotwords_route_returns_dictionary_without_forwarding_input(self):
        payload = {
            "words": [{"text": "爱情公寓", "pinyin": "aiqinggongyu", "initials": "aqgy",
                       "kind": "tv", "weight": 100}],
            "updated_at": 123, "source": "cache", "stale": True,
        }
        service = Mock()
        service.get.return_value = payload
        with patch.object(proxy, "_get_ime_hotword_service", return_value=service, create=True), \
                patch.object(proxy, "_get_vod_service", return_value=Mock()):
            with urllib.request.urlopen(self.url + "?q=private-input", timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), payload)
                self.assertIn("application/json", response.headers["Content-Type"])
                self.assertIn("no-store", response.headers["Cache-Control"])
            service.get.assert_called_once_with()

    def test_initialization_failure_returns_readable_json(self):
        with patch.object(proxy, "_get_ime_hotword_service", side_effect=OSError("unavailable"),
                          create=True), \
                patch.object(proxy, "_get_vod_service", return_value=Mock()):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.url, timeout=3)
            self.assertEqual(error.exception.code, 503)
            self.assertFalse(json.loads(error.exception.read())["ok"])


if __name__ == "__main__":
    unittest.main()

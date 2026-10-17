"""Selected VOD routes never silently resolve a different provider."""
import http.server
import json
import pathlib
import sys
import threading
import unittest
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import proxy


class QuietHandler(proxy.Handler):
    def log_message(self, *args):
        pass


class ManualVodSourceTest(unittest.TestCase):
    def setUp(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.base = "http://127.0.0.1:%d" % server.server_address[1]
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 3)
        self.addCleanup(server.shutdown)

    def request(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as response:
            return json.load(response)

    def test_native_failure_never_resolves_a_supplemental_source(self):
        supplemental = {"sources": [{"episodes": [{"pid": 100, "vid": 3}]}]}
        for result in (None, OSError("unavailable")):
            with self.subTest(result=result), \
                    patch.object(proxy, "_hhkan_native_play", side_effect=result if isinstance(result, Exception) else None,
                                 return_value=None) as native, \
                    patch.object(proxy._HHKAN_SNAPSHOT, "detail", return_value=supplemental) as snapshot, \
                    patch.object(proxy, "enrich_detail_sources") as enrich, \
                    patch.object(proxy, "mcms_enriched_play") as alternate:
                payload = self.request("/hhkan/play/71234/9/3?fresh=1")
                self.assertEqual(payload["sources"], [])
                self.assertIn("error", payload)
                native.assert_called_once_with(71234, 9, 3, fresh=True)
                snapshot.assert_not_called()
                enrich.assert_not_called()
                alternate.assert_not_called()

    def test_manually_selected_supplemental_source_is_still_resolved(self):
        chosen = {"line": "manual", "sources": [{"url": "https://chosen.test/movie.m3u8"}]}
        with patch.object(proxy, "mcms_enriched_play", return_value=chosen) as selected, \
                patch.object(proxy, "_hhkan_native_play") as native:
            self.assertEqual(self.request("/hhkan/play/71234/100/3?fresh=1"), chosen)
            selected.assert_called_once_with(71234, 100, 3, fresh=True)
            native.assert_not_called()


if __name__ == "__main__":
    unittest.main()

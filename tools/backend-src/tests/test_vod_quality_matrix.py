import pathlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


QA_DIR = pathlib.Path(__file__).resolve().parents[2] / "qa"
sys.path.insert(0, str(QA_DIR))
import vod_quality_matrix as matrix  # noqa: E402


class MatrixHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/search":
            page = int(query.get("page", ["1"])[0])
            if page == 1:
                items, more = [{"id": "douban:11", "title": "红楼梦 1987"}], True
            else:
                items, more = [
                    {"id": "douban:11", "title": "红楼梦 1987 重复"},
                    {"id": "douban:12", "title": "红楼梦 1996"},
                ], False
            return self.send(200, (('{"items":%s,"total":2,"has_more":%s}' %
                                    (__import__("json").dumps(items, ensure_ascii=False),
                                     "true" if more else "false"))).encode())
        if parsed.path == "/hhkan/search":
            return self.send(200, b'{"items":[{"id":99,"title":"\xe7\xba\xa2\xe6\xa5\xbc\xe6\xa2\xa6 HHKan"}]}')
        if parsed.path == "/vod/sources/probe":
            return self.send(200, b'{"ok":1,"failed":1,"results":[{"name":"healthy","ok":true,"latencyMs":12,"status":200,"error":""},{"name":"unstable","ok":false,"latencyMs":34,"status":0,"error":"URLError https://secret.example/path?token=hidden"},{"name":"hhkan","ok":false,"latencyMs":0,"status":0,"error":"adapter not probeable"}]}')
        if parsed.path == "/vod/detail/11":
            return self.send(200, b'{"title":"\xe7\xba\xa2\xe6\xa5\xbc\xe6\xa2\xa6","sources":[{"id":"line-1","name":"\xe7\xa8\xb3\xe5\xae\x9a\xe7\xba\xbf","episodes":[{"name":"1","url":"x"},{"name":"2","url":"x"}]}]}')
        if parsed.path == "/vod/detail/12":
            return self.send(200, b'{"title":"\xe7\xba\xa2\xe6\xa5\xbc\xe6\xa2\xa6","sources":[{"id":"line-1","name":"\xe7\xa8\xb3\xe5\xae\x9a\xe7\xba\xbf","episodes":[{"name":"1","url":"x"},{"name":"2","url":"x"}]}]}')
        if parsed.path == "/hhkan/detail/99":
            return self.send(200, b'{"title":"\xe7\xba\xa2\xe6\xa5\xbc\xe6\xa2\xa6","sources":[{"name":"\xe5\xa5\xbd\xe5\xa5\xbd\xe7\x9c\x8b\xe7\xba\xbf","episodes":[{"pid":7,"vid":0},{"pid":7,"vid":1}]}]}')
        if parsed.path.startswith("/vod/play/"):
            return self.send(200, ('{"sources":[{"url":"http://127.0.0.1:%d/media.m3u8"}]}' %
                                   self.server.server_port).encode())
        if parsed.path.startswith("/hhkan/play/99/7/"):
            return self.send(200, ('{"sources":[{"url":"http://127.0.0.1:%d/media.m3u8"}]}' %
                                   self.server.server_port).encode())
        if parsed.path == "/media.m3u8":
            return self.send(200, b'#EXTM3U\n#EXTINF:4,\nfirst.ts\n', "application/vnd.apple.mpegurl")
        if parsed.path == "/first.ts":
            return self.send(200, b"\x47" * 128, "video/mp2t")
        return self.send(404, b"not found", "text/plain")


class VodQualityMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MatrixHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%s" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_collects_pages_and_keeps_same_title_works(self):
        api = matrix._search_pages(self.base, "红楼梦", "api", 100, 5, 2)
        self.assertEqual(api["pages"], 2)
        self.assertEqual(api["raw_count"], 3)
        self.assertEqual(api["unique_count"], 2)
        self.assertEqual(api["duplicate_count"], 1)

    def test_source_sampling_includes_the_last_backup_line(self):
        self.assertEqual(matrix._line_indexes([0, 1, 2, 3, 4], 2), [0, 4])
        self.assertEqual(matrix._line_indexes([0, 1, 2, 3, 4], 3), [0, 2, 4])

    def test_source_health_round_sanitizes_errors_and_excludes_non_probeable(self):
        report = matrix._probe_source_round(self.base, 2)
        self.assertEqual((report["checked"], report["healthy"], report["failed"]), (3, 1, 1))
        rendered = __import__("json").dumps(report)
        self.assertNotIn("secret.example", rendered)
        self.assertNotIn("token=hidden", rendered)

    def test_playback_matrix_probes_hls_manifest_and_first_segment(self):
        report = matrix.run_matrix(self.base, ["红楼梦"], max_items=2, max_lines=2,
                                   episodes_per_line=2, timeout=2, probe_streams=True)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["summary"]["works_checked"], 2)
        self.assertEqual(report["summary"]["play_requests"], 4)
        self.assertEqual(report["summary"]["media_probes_ok"], 4)
        self.assertIn("hhkan:99", [work["id"] for work in report["queries"][0]["works"]])
        rendered = __import__("json").dumps(report, ensure_ascii=False)
        self.assertNotIn("/media.m3u8", rendered)


if __name__ == "__main__":
    unittest.main()

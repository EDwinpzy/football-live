import json
import pathlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import metadata_client  # noqa: E402


class _MetadataHandler(BaseHTTPRequestHandler):
    payload = {}
    status = 200
    paths = []

    def do_GET(self):
        type(self).paths.append(self.path)
        body = json.dumps(type(self).payload).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class MetadataClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _MetadataHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _MetadataHandler.status = 200
        _MetadataHandler.paths = []
        _MetadataHandler.payload = {
            "id": 10000123,
            "mytv_id": 10000123,
            "title": "早春晴朗",
            "media_type": "tv",
            "episodes": 24,
            "external_ids": {"douban": "1292052"},
        }
        self.env = patch.dict("os.environ", {"OTV_METADATA_API_URL": self.origin})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_detail_uses_configured_metadata_origin_and_requested_id(self):
        detail = metadata_client.MetadataClient().detail(10000123)

        self.assertEqual(detail["mytv_id"], 10000123)
        self.assertEqual(detail["external_ids"]["douban"], "1292052")
        self.assertEqual(_MetadataHandler.paths, ["/metadata/10000123"])

    def test_missing_record_is_reported_as_not_found(self):
        _MetadataHandler.status = 404

        self.assertIsNone(metadata_client.MetadataClient().detail(999))
        self.assertEqual(_MetadataHandler.paths, ["/metadata/999"])

    def test_detail_rejects_a_response_for_a_different_mytv_id(self):
        _MetadataHandler.payload = {
            "id": 10000124,
            "mytv_id": 10000124,
            "title": "另一部片",
            "external_ids": {"douban": "1292052"},
        }

        with self.assertRaises(metadata_client.MetadataIdentityConflict):
            metadata_client.MetadataClient().detail(10000123)

    def test_metadata_api_failure_does_not_become_a_fabricated_record(self):
        _MetadataHandler.status = 503

        with self.assertRaises(metadata_client.MetadataUnavailable):
            metadata_client.MetadataClient().detail(10000123)


if __name__ == "__main__":
    unittest.main()

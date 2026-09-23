import json
import re
import socket
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "selfhosted"))

from health import make_server  # noqa: E402


class SelfHostedHealthTests(unittest.TestCase):
    def _service_block(self, compose, name):
        match = re.search(
            rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9_-]*:\n|\Z)",
            compose,
        )
        self.assertIsNotNone(match, f"missing Compose service {name}")
        return match.group(1)

    def _close_server(self, server, thread):
        server.shutdown()
        thread.join(1)
        server.server_close()

    def _server(self, database_port, objects_root):
        env = {
            "DATABASE_HOST": "127.0.0.1",
            "DATABASE_PORT": str(database_port),
            "OBJECTS_ROOT": str(objects_root),
        }
        server = make_server("127.0.0.1", 0, env)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._close_server, server, thread)
        return server

    def test_health_is_ok_when_database_and_object_root_are_available(self):
        with socket.socket() as database:
            database.bind(("127.0.0.1", 0))
            database.listen()
            with tempfile.TemporaryDirectory() as objects:
                server = self._server(database.getsockname()[1], objects)
                conn = HTTPConnection(*server.server_address)
                conn.request("GET", "/health")
                response = conn.getresponse()
                payload = json.loads(response.read())
                conn.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"status": "ok"})

    def test_health_is_unavailable_when_database_is_unreachable(self):
        with tempfile.TemporaryDirectory() as objects:
            with socket.socket() as unused:
                unused.bind(("127.0.0.1", 0))
                closed_port = unused.getsockname()[1]
            server = self._server(closed_port, objects)
            conn = HTTPConnection(*server.server_address)
            conn.request("GET", "/health")
            response = conn.getresponse()
            payload = json.loads(response.read())
            conn.close()

        self.assertEqual(response.status, 503)
        self.assertEqual(payload, {"status": "unavailable"})

    def test_health_is_unavailable_when_required_configuration_is_missing(self):
        server = make_server("127.0.0.1", 0, {})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._close_server, server, thread)

        conn = HTTPConnection(*server.server_address)
        conn.request("GET", "/health")
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()

        self.assertEqual(response.status, 503)
        self.assertEqual(payload, {"status": "unavailable"})

    def test_compose_keeps_database_private_and_persists_database_and_objects(self):
        compose = (ROOT.parent / "deploy" / "selfhosted" / "compose.yaml").read_text(encoding="utf-8")
        postgres_service = self._service_block(compose, "postgres")
        self.assertNotIn("ports:", postgres_service)
        self.assertIn("postgres_data:", compose)
        self.assertIn("OBJECTS_ROOT", compose)
        self.assertIn("nginx:", compose)
        for service in ("auth", "postgrest"):
            block = self._service_block(compose, service)
            self.assertNotIn("ports:", block)

    def test_business_api_is_private_and_reads_only_the_mytv_object_root(self):
        compose = (ROOT.parent / "deploy" / "selfhosted" / "compose.yaml").read_text(encoding="utf-8")
        api = self._service_block(compose, "business-api")
        self.assertNotIn("ports:", api)
        self.assertIn("OTV_POSTGREST_RPC_URL: http://postgrest:3000/rpc", api)
        self.assertIn("${OBJECTS_HOST_PATH:-./data/objects}:/srv/objects:ro", api)
        self.assertIn("/app/api_service.py:ro", api)

    def test_http_staging_proxy_does_not_expose_auth_or_rpc_routes(self):
        nginx = (ROOT.parent / "deploy" / "selfhosted" / "nginx.conf").read_text(encoding="utf-8")
        self.assertIn("location = /health", nginx)
        self.assertIn("location / {\n        return 404;", nginx)
        self.assertNotIn("/auth/v1/signin", nginx)
        self.assertNotIn("/v1/rdb/rest/", nginx)

    def test_storage_service_is_private_and_uses_only_mytv_bucket(self):
        compose = (ROOT.parent / "deploy" / "selfhosted" / "compose.yaml").read_text(encoding="utf-8")
        storage = self._service_block(compose, "storage")
        self.assertNotIn("ports:", storage)
        self.assertIn("expose:", storage)
        self.assertIn("OTV_STORAGE_BUCKETS: hotupdate", storage)
        self.assertIn("${OBJECTS_HOST_PATH:-./data/objects}:/srv/objects", storage)


if __name__ == "__main__":
    unittest.main()

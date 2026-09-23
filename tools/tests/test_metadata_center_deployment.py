import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))


class MetadataCenterDeploymentTests(unittest.TestCase):
    def _service_block(self, compose, name):
        match = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9_-]*:\n|\Z)", compose)
        self.assertIsNotNone(match, f"missing Compose service {name}")
        return match.group(1)

    def test_compose_defines_metadata_services(self):
        compose = (REPO / "deploy" / "selfhosted" / "compose.yaml").read_text(encoding="utf-8")
        api = self._service_block(compose, "metadata-api")
        self.assertIn("metadata_center.read_api", api)
        self.assertIn("OTV_METADATA_PORT", api)
        self.assertIn("OTV_METADATA_DIR", api)
        self.assertIn("OTV_METADATA_PUBLIC_DIR", api)
        builder = self._service_block(compose, "metadata-builder")
        self.assertIn('profiles: ["ops"]', builder)
        self.assertIn("metadata_center.cli", builder)
        refresh = self._service_block(compose, "metadata-refresh")
        self.assertIn("refresh.sh", refresh)
        self.assertIn("build_douban_snapshot.py", refresh)

    def test_nginx_routes_metadata_path_to_service(self):
        nginx = (REPO / "deploy" / "selfhosted" / "nginx.conf").read_text(encoding="utf-8")
        self.assertRegex(nginx, r"location \^~ /metadata/ \{")
        self.assertIn("proxy_pass http://metadata-api:8094", nginx)

    def test_refresh_runner_uses_snapshot_fallback(self):
        script = (ROOT / "metadata_center" / "refresh.sh").read_text(encoding="utf-8")
        self.assertIn("build_douban_snapshot.py", script)
        self.assertIn("rebuild", script)
        # A failed snapshot refresh must not block republishing the last good data.
        self.assertIn("|| true", script)


if __name__ == "__main__":
    unittest.main()

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
import yaml

from tools.selfhosted.admin_config import load_admin_credentials, load_session_secret

ROOT = Path(__file__).resolve().parents[1]


class SelfHostedAdminRuntimeTests(unittest.TestCase):
    def test_admin_credentials_can_be_loaded_from_a_private_runtime_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "admin.json"
            path.write_text(json.dumps({"username": "operator", "password": "long-runtime-password"}), encoding="utf-8")
            if os.name == "posix":
                path.chmod(0o600)
            self.assertEqual(
                load_admin_credentials({"OTV_ADMIN_CREDENTIALS_FILE": str(path)}),
                ("operator", "long-runtime-password"),
            )

    def test_admin_environment_credentials_must_be_provided_as_a_pair(self):
        with self.assertRaisesRegex(ValueError, "both"):
            load_admin_credentials({"OTV_ADMIN_USER": "operator"})

    def test_admin_credentials_reject_group_or_world_readable_file(self):
        if os.name != "posix":
            self.skipTest("POSIX file mode check")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "admin.json"
            path.write_text(json.dumps({"username": "operator", "password": "long-runtime-password"}), encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "permissions"):
                load_admin_credentials({"OTV_ADMIN_CREDENTIALS_FILE": str(path)})

    def test_session_secret_file_requires_at_least_32_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-secret"
            path.write_text("short", encoding="utf-8")
            if os.name == "posix":
                path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "32 bytes"):
                load_session_secret({"OTV_ADMIN_SECRET_FILE": str(path)})

    def test_session_secret_is_loaded_from_private_runtime_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-secret"
            expected = "s" * 48
            path.write_text(expected, encoding="utf-8")
            if os.name == "posix":
                path.chmod(0o600)
            self.assertEqual(load_session_secret({"OTV_ADMIN_SECRET_FILE": str(path)}), expected)

    def test_admin_compose_service_is_private_and_mounts_runtime_secrets_read_only(self):
        compose = (ROOT.parent / "deploy" / "selfhosted" / "compose.yaml").read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^  admin:\n(.*?)(?=^  [a-z][a-z0-9_-]*:\n|\Z)", compose
        )
        self.assertIsNotNone(match, "missing Compose admin service")
        service = match.group(1)
        self.assertNotIn("ports:", service)
        self.assertIn("OTV_SELFHOSTED_MODE: \"1\"", service)
        self.assertIn("/run/secrets/admin-credentials.json:ro", service)
        self.assertIn("/run/secrets/admin-session-secret:ro", service)
        self.assertIn("/run/secrets/admin-api-credentials.json:ro", service)
        self.assertIn("/run/secrets/license:ro", service)
        self.assertIn("http://postgrest:3000", service)

    def test_clean_compose_builds_admin_and_bootstraps_postgres_roles(self):
        compose = yaml.safe_load((ROOT.parent / "deploy" / "selfhosted" / "compose.yaml").read_text(encoding="utf-8"))
        admin = compose["services"]["admin"]
        self.assertEqual(
            admin["build"],
            {"context": "../..", "dockerfile": "deploy/selfhosted/Dockerfile.admin"},
        )
        postgres_volumes = compose["services"]["postgres"]["volumes"]
        self.assertIn(
            "../../tools/selfhosted/db_bootstrap.sql:/docker-entrypoint-initdb.d/00-db-bootstrap.sql:ro",
            postgres_volumes,
        )
        self.assertIn(
            "../../tools/selfhosted/iptv_publish_lock.sql:/docker-entrypoint-initdb.d/iptv_publish_lock.sql:ro",
            postgres_volumes,
        )

    def test_admin_gets_only_the_license_importer_database_credentials(self):
        compose = yaml.safe_load((ROOT.parent / "deploy" / "selfhosted" / "compose.yaml").read_text(encoding="utf-8"))
        admin = compose["services"]["admin"]
        environment = admin["environment"]
        for key in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "POSTGRES_USER", "POSTGRES_PASSWORD"):
            self.assertNotIn(key, environment, f"Admin must not receive PostgreSQL bootstrap credential {key}")
        self.assertEqual(environment["OTV_LICENSE_IMPORT_DB"], "${POSTGRES_DB:-optimal_tv}")
        self.assertEqual(environment["OTV_LICENSE_IMPORT_USER"], "license_importer")
        self.assertEqual(environment["OTV_LICENSE_IMPORT_PASSWORD_FILE"], "/run/secrets/license-importer-db-password")
        self.assertIn(
            "${LICENSE_IMPORT_PASSWORD_HOST_PATH:-/srv/mytv/secrets/license-importer-db-password}:/run/secrets/license-importer-db-password:ro",
            admin["volumes"],
        )

    def test_admin_image_does_not_copy_runtime_private_keys(self):
        dockerfile = (ROOT.parent / "deploy" / "selfhosted" / "Dockerfile.admin").read_text(encoding="utf-8")
        self.assertIn("tools/admin_server.py", dockerfile)
        self.assertIn("tools/admin.html", dockerfile)
        self.assertIn("tools/apk_update.py", dockerfile)
        self.assertIn("tools/selfhosted/admin_config.py", dockerfile)
        self.assertIsNone(re.search(r"(?m)^COPY .*tools/keys", dockerfile))

    def test_admin_identity_labels_describe_the_self_hosted_importer(self):
        html = (ROOT / "admin.html").read_text(encoding="utf-8")
        self.assertNotIn("云端 appletv-", html)
        self.assertNotIn("云端身份 svc_import", html)
        self.assertIn("自建实例 · 登录会话 12h", html)
        self.assertIn("license_importer", html)


if __name__ == "__main__":
    unittest.main()

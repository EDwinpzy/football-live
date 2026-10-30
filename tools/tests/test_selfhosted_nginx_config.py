from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "selfhosted"


class QiuJiLiveIngressConfigTests(unittest.TestCase):
    def require(self, needle: str, content: str, label: str) -> None:
        self.assertIn(needle, content, f"missing ingress contract: {label}")

    def test_tls_is_published_only_through_the_nginx_gateway(self):
        compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
        nginx = (DEPLOY / "nginx.conf").read_text(encoding="utf-8")
        for value, label in (
            ("${TLS_HOST_PORT:-8443}:8443", "TLS host port"),
            ("/run/secrets/server.crt:ro", "read-only server certificate"),
            ("/run/secrets/server.key:ro", "read-only server key"),
        ):
            self.require(value, compose, label)
        self.assertNotIn("mytv-ca.key", compose, "CA signing key must stay outside containers")
        for value, label in (
            ("listen 8443 ssl;", "TLS listener"),
            ("ssl_certificate /run/secrets/server.crt;", "server certificate path"),
            ("ssl_certificate_key /run/secrets/server.key;", "server private key path"),
        ):
            self.require(value, nginx, label)

    def test_only_native_health_update_and_object_routes_are_public(self):
        nginx = (DEPLOY / "nginx.conf").read_text(encoding="utf-8")
        for value, label in (
            ("location = /health", "health endpoint"),
            ("location = /hotupdate", "runtime update check"),
            ("proxy_pass http://business-api:8082/hotupdate;", "runtime update API"),
            ("location ^~ /hotupdate/", "runtime update reports"),
            ("location ^~ /v1/storages/object/", "update object storage"),
            ("location = /web/apk-update/tv.json", "TV update manifest compatibility URL"),
            ("location = /web/apk-update/mobile.json", "mobile update manifest compatibility URL"),
            (
                "proxy_pass http://storage:8092/v1/storages/object/hotupdate/apk-update/tv.json;",
                "TV manifest storage route",
            ),
            (
                "proxy_pass http://storage:8092/v1/storages/object/hotupdate/apk-update/mobile.json;",
                "mobile manifest storage route",
            ),
            ("location / {\n        return 404;", "unknown routes rejected"),
        ):
            self.require(value, nginx, label)
        self.assertNotIn("proxy_pass http://web-api", nginx)
        self.assertNotIn("return 308 /web/", nginx)
        self.assertNotIn("location = /web {", nginx)
        for removed_route in ("/activate", "/iptv/", "/metadata/", "/admin/"):
            self.assertNotIn(removed_route, nginx)

    def test_runtime_services_contain_only_native_and_update_dependencies(self):
        compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
        config = yaml.safe_load(compose)
        services = config["services"]
        self.assertNotIn("admin", services)
        self.assertNotIn("web-api", services)
        self.assertFalse(any(name.startswith("metadata-") for name in services))
        self.assertNotIn("Dockerfile.web", compose)
        self.assertNotIn("build_web_assets.py", compose)
        self.assertNotIn("WEB_ASSET_ARCHIVE", compose)
        self.assertFalse((DEPLOY / "Dockerfile.web").exists())
        proxy = (ROOT / "tools" / "backend-src" / "proxy.py").read_text(encoding="utf-8")
        for setting in ("OTV_IPTV_LATEST_URL", "OTV_LICENSE_ENDPOINT", "OTV_METADATA_API_URL"):
            self.assertNotIn(setting, proxy)

    def test_database_api_grants_only_football_hotupdate_rpcs(self):
        repair = (ROOT / "tools" / "selfhosted" / "rpc_grants_repair.sql").read_text(encoding="utf-8")
        for statement in (
            "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;",
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC, anon, authenticated;",
            "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, anon, authenticated;",
            "GRANT EXECUTE ON FUNCTION public.hotupdate_check(integer, integer) TO anon, authenticated;",
            "GRANT EXECUTE ON FUNCTION public.hotupdate_report(text, integer, integer, integer, text, text)",
        ):
            self.require(statement, repair, statement)
        for removed_rpc in ("activate_code", "admin_", "announcement_", "iptv_", "vod_source"):
            self.assertNotIn(removed_rpc, repair)
        migration_runner = (ROOT / "tools" / "selfhosted" / "apply_schema_migrations.sh").read_text(encoding="utf-8")
        self.assertNotIn("LICENSE_IMPORTER_PASSWORD_FILE", migration_runner)
        self.assertNotIn("license_importer_smoke.psql", migration_runner)
        retired_role = (ROOT / "tools" / "selfhosted" / "license_importer_role.sql").read_text(encoding="utf-8")
        self.require("NOLOGIN", retired_role, "retired importer account disabled")
        self.assertNotIn("GRANT ", retired_role)

    def test_tv_and_mobile_apps_trust_the_same_private_ca(self):
        tv_ca = ROOT / "android" / "app" / "src" / "main" / "res" / "raw" / "mytv_migration_ca.pem"
        mobile_ca = ROOT / "internal" / "mobile" / "app" / "src" / "main" / "res" / "raw" / "mytv_migration_ca.pem"
        self.assertEqual(tv_ca.read_bytes(), mobile_ca.read_bytes())


if __name__ == "__main__":
    unittest.main()

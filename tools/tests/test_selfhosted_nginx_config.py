from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "selfhosted"


class SelfHostedIngressConfigTests(unittest.TestCase):
    def require(self, needle: str, content: str, label: str) -> None:
        self.assertIn(needle, content, f"missing ingress contract: {label}")

    def test_tls_is_published_only_through_the_nginx_gateway(self):
        compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
        nginx = (DEPLOY / "nginx.conf").read_text(encoding="utf-8")
        for value, label in (
            ("${TLS_HOST_PORT:-8443}:8443", "NAT HTTPS target"),
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

    def test_my_tv_routes_reach_the_right_private_services(self):
        nginx = (DEPLOY / "nginx.conf").read_text(encoding="utf-8")
        expected = (
            ("location = /activate", "proxy_pass http://business-api:8082/activate;"),
            ("location = /hotupdate", "proxy_pass http://business-api:8082/hotupdate;"),
            ("location ^~ /hotupdate/", "proxy_pass http://business-api:8082;"),
            ("location ^~ /iptv/", "proxy_pass http://business-api:8082;"),
            ("location ^~ /v1/storages/object/", "proxy_pass http://storage:8092;"),
            ("location = /web", "return 308 /web/;"),
            ("location ^~ /web/", "proxy_pass http://web-api:8090/;"),
            ("location /", "proxy_pass http://admin:8791;"),
        )
        for location, target in expected:
            with self.subTest(location=location):
                self.require(location, nginx, location)
                self.require(target, nginx, target)

    def test_web_proxy_uses_the_self_hosted_business_and_file_services(self):
        compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
        dockerfile = (DEPLOY / "Dockerfile.web").read_text(encoding="utf-8")
        for value, label in (
            ("web-api:", "web API service"),
            ("dockerfile: deploy/selfhosted/Dockerfile.web", "web API image"),
            ("OTV_CLOUD_RELAY: \"0\"", "no CloudBase relay dependency"),
            ("OTV_LICENSE_ENDPOINT: http://business-api:8082/activate", "local activation API"),
            ("OTV_APK_UPDATE_OBJECT_BASE: http://storage:8092/v1/storages/object/hotupdate", "local file storage"),
            ("OTV_IPTV_LATEST_URL: http://business-api:8082/iptv/latest", "local IPTV API"),
            ("OTV_VOD_SOURCE_CONFIG_URL: http://business-api:8082/internal/vod-sources", "private published VOD configuration"),
        ):
            self.require(value, compose, label)
        self.require("python:3.12-slim", dockerfile, "Python 3.12 runtime")
        self.require("COPY cloudfunctions/otv-web/vendor/ /app/vendor/", dockerfile, "vendored QuickJS runtime")
        self.require("cp /app/vendor/_quickjs.cpython-311-x86_64-linux-gnu.so /app/vendor/_quickjs.so", dockerfile,
                     "QuickJS extension loader name")
        proxy = (ROOT / "tools" / "backend-src" / "proxy.py").read_text(encoding="utf-8")
        self.require("OTV_IPTV_LATEST_URL", proxy, "self-hosted IPTV endpoint override")
        self.require('host == "business-api"', proxy, "private Compose IPTV source allowlist")

    def test_tv_and_mobile_apps_trust_the_same_private_ca(self):
        tv_ca = ROOT / "android" / "app" / "src" / "main" / "res" / "raw" / "mytv_migration_ca.pem"
        mobile_ca = ROOT / "internal" / "mobile" / "app" / "src" / "main" / "res" / "raw" / "mytv_migration_ca.pem"
        self.assertEqual(tv_ca.read_bytes(), mobile_ca.read_bytes())


if __name__ == "__main__":
    unittest.main()

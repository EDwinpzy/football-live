import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.selfhosted.api_service import BusinessApi, PostgrestRpcClient  # noqa: E402


class FakeRpc:
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def __call__(self, name, payload):
        self.calls.append((name, payload))
        result = self.results.get(name, {"ret": 0})
        if isinstance(result, Exception):
            raise result
        return result


class SelfHostedApiContractTests(unittest.TestCase):
    def make_api(self, results=None, **kwargs):
        rpc = FakeRpc(results)
        api = BusinessApi(rpc, **kwargs)
        return api, rpc

    def test_activation_v2_forwards_the_existing_rpc_contract(self):
        response = {"ret": 0, "ticket": {"v": 1}, "licenseCode": "OTV-ABCDE-23456"}
        api, rpc = self.make_api({"activate_code": response})
        status, body, headers = api.handle("POST", "/activate", {
            "protocol": 2,
            "action": "activate",
            "code": "otv-abcde-23456",
            "deviceId": "a" * 64,
        }, {"X-Forwarded-For": "192.0.2.8, 10.0.0.2"})

        self.assertEqual(status, 200)
        self.assertEqual(body, response)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(rpc.calls, [("activate_code", {
            "p_protocol": 2,
            "p_action": "activate",
            "p_code": "OTV-ABCDE-23456",
            "p_device": "a" * 64,
            "p_current_code": None,
            "p_ip": "192.0.2.8",
        })])

    def test_activation_rejects_invalid_protocol_and_card_without_rpc(self):
        api, rpc = self.make_api()
        self.assertEqual(api.handle("POST", "/activate", {"protocol": 1})[1],
                         {"ret": 426, "msg": "upgrade_required"})
        self.assertEqual(api.handle("POST", "/activate", {
            "protocol": 2, "action": "activate", "code": "OTV-00000-00000", "deviceId": "a" * 64,
        })[1], {"ret": 400, "msg": "bad_code"})
        self.assertEqual(rpc.calls, [])

    def test_restore_forwards_device_without_an_activation_card_code(self):
        response = {
            "ret": 0, "ticket": {"v": 1}, "licenseCode": "OTV-ABCDE-23456",
            "activatedAt": "2026-01-01T00:00:00Z", "expireAt": "2026-10-01T00:00:00Z",
        }
        api, rpc = self.make_api({"activate_code": response})
        status, body, _headers = api.handle("POST", "/activate", {
            "protocol": 2, "action": "restore", "code": "", "deviceId": "a" * 64,
        })

        self.assertEqual(status, 200)
        self.assertEqual(body, response)
        self.assertEqual(rpc.calls, [("activate_code", {
            "p_protocol": 2, "p_action": "restore", "p_code": None,
            "p_device": "a" * 64, "p_current_code": None, "p_ip": None,
        })])

    def test_hotupdate_check_preserves_rpc_payload_and_public_download_url(self):
        response = {"ret": 0, "hasUpdate": True, "pkg": {"objectKey": "backend-hu21.zip"}}
        api, rpc = self.make_api({"hotupdate_check": response}, public_base_url="https://tv.example")
        status, body, _ = api.handle("POST", "/hotupdate/check", {
            "hotVersion": "4", "apkVersion": "23", "deviceId": "device_123",
        })
        self.assertEqual(status, 200)
        self.assertEqual(rpc.calls, [("hotupdate_check", {"p_hot_version": 4, "p_apk_version": 23})])
        self.assertEqual(body["pkg"]["url"], "https://tv.example/v1/storages/object/hotupdate/backend-hu21.zip")

    def test_hotupdate_blackout_skips_the_database_rpc(self):
        api, rpc = self.make_api(hotupdate_off=True)
        _, body, _ = api.handle("POST", "/hotupdate/check", {
            "hotVersion": 1, "apkVersion": 23, "deviceId": "device_123",
        })
        self.assertEqual(body, {"ret": 0, "hasUpdate": False, "blackout": True})
        self.assertEqual(rpc.calls, [])

    def test_announcement_uses_the_existing_response_envelope_and_cache(self):
        api, rpc = self.make_api({"announcement_latest": {"items": []}})
        first = api.handle("GET", "/hotupdate/announce")
        second = api.handle("GET", "/hotupdate/announce")
        self.assertEqual(first[1], {"ret": 0, "items": []})
        self.assertEqual(first[2]["Cache-Control"], "public, max-age=60, stale-while-revalidate=300")
        self.assertEqual(second[1], first[1])
        self.assertEqual(len(rpc.calls), 1)

    def test_hotupdate_report_clips_untrusted_text_before_rpc(self):
        api, rpc = self.make_api()
        api.handle("POST", "/hotupdate/report", {
            "deviceId": "device_123", "result": "x" * 40, "detail": "y" * 600,
        })
        self.assertEqual(rpc.calls, [("hotupdate_report", {
            "p_device": "device_123", "p_apk_version": None, "p_from_version": None,
            "p_to_version": None, "p_result": "x" * 32, "p_detail": "y" * 500,
        })])

    def test_vod_source_check_forwards_version_and_device_validation(self):
        api, rpc = self.make_api({"vod_source_public_check": {"ret": 0, "version": 4}})
        self.assertEqual(api.handle("POST", "/hotupdate/vod-sources/check", {
            "version": "4", "deviceId": "device_123",
        })[1], {"ret": 0, "version": 4})
        self.assertEqual(rpc.calls, [("vod_source_public_check", {"p_version": 4})])
        self.assertEqual(api.handle("POST", "/hotupdate/vod-sources/check", {"version": 4})[1],
                         {"ret": 400, "msg": "bad_device"})

    def test_internal_vod_config_returns_only_published_database_config(self):
        published = {
            "ret": 0, "hasUpdate": True, "version": 4, "sha256": "a" * 64,
            "config": {"schema": 1, "version": 4, "sources": []},
        }
        api, rpc = self.make_api({"vod_source_public_check": published})
        status, body, headers = api.handle("GET", "/internal/vod-sources")
        self.assertEqual(status, 200)
        self.assertEqual(body, published)
        self.assertEqual(headers["Cache-Control"], "private, max-age=30")
        self.assertEqual(rpc.calls, [("vod_source_public_check", {"p_version": 0})])
        self.assertEqual(api.handle("POST", "/internal/vod-sources", {})[1],
                         {"ret": 404, "msg": "not_found"})
        self.assertEqual(len(rpc.calls), 1)

    def test_iptv_latest_and_status_serve_only_mytv_hotupdate_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hotupdate" / "iptv"
            root.mkdir(parents=True)
            (root / "iptv-latest.m3u").write_text("#EXTM3U\\n", encoding="utf-8")
            (root / "version.json").write_text('{"version":2}', encoding="utf-8")
            api, _ = self.make_api(object_root=tmp)
            latest = api.handle("GET", "/iptv/latest")
            status = api.handle("GET", "/iptv/status")
            self.assertEqual(latest[0], 200)
            self.assertEqual(latest[1], b"#EXTM3U\\n")
            self.assertIn("audio/x-mpegurl", latest[2]["Content-Type"])
            self.assertEqual(status[1], {"version": 2})

    def test_iptv_refresh_accepts_the_existing_header_or_query_token(self):
        api, _ = self.make_api(refresh_token="staging-secret", refresh=lambda: {"ret": 0, "channels": 1})
        header_result = api.handle("POST", "/iptv/refresh", {}, {"X-OTV-Token": "staging-secret"})
        query_result = api.handle("POST", "/iptv/refresh?token=staging-secret", {})
        self.assertEqual(header_result[1], {"ret": 0, "channels": 1})
        self.assertEqual(query_result[1], {"ret": 0, "channels": 1})

    def test_rpc_client_refuses_cloudbase_hosts_and_nonpublic_rpcs(self):
        with self.assertRaisesRegex(ValueError, "private self-hosted endpoint"):
            PostgrestRpcClient("https://appletv.api.tcloudbasegateway.com/v1/rdb/rest/rpc")
        client = PostgrestRpcClient("http://postgrest:3000/rpc")
        with self.assertRaisesRegex(ValueError, "RPC is not public"):
            client("hotupdate_admin_delete", {"p_hot_version": 1})

    def test_public_origin_refuses_cloudbase_and_plain_http(self):
        with self.assertRaisesRegex(ValueError, "HTTPS origin"):
            BusinessApi(FakeRpc(), public_base_url="http://new-server.example")
        with self.assertRaisesRegex(ValueError, "CloudBase URLs"):
            BusinessApi(FakeRpc(), public_base_url="https://example.tcloudbase.com")

    def test_unknown_routes_remain_not_found_and_options_is_cors_safe(self):
        api, _ = self.make_api()
        self.assertEqual(api.handle("GET", "/unlisted")[0], 404)
        status, _, headers = api.handle("OPTIONS", "/activate")
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Methods"], "GET, POST, OPTIONS")


if __name__ == "__main__":
    unittest.main()

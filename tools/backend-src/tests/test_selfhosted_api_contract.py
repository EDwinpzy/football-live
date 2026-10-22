import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.selfhosted.api_service import BusinessApi, PostgrestRpcClient, RPC_ALLOWLIST  # noqa: E402


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

    def test_rpc_allowlist_contains_only_client_hotupdate_contracts(self):
        self.assertEqual(RPC_ALLOWLIST, {"hotupdate_check", "hotupdate_report"})
        client = PostgrestRpcClient("http://postgrest:3000/rpc")
        for retired_rpc in ("activate_code", "announcement_latest", "vod_source_public_check"):
            with self.subTest(rpc=retired_rpc), self.assertRaisesRegex(ValueError, "RPC is not public"):
                client(retired_rpc, {})

    def test_hotupdate_check_preserves_rpc_payload_and_public_download_url(self):
        response = {"ret": 0, "hasUpdate": True, "pkg": {"objectKey": "backend-hu21.zip"}}
        api, rpc = self.make_api({"hotupdate_check": response}, public_base_url="https://tv.example")
        status, body, _headers = api.handle("POST", "/hotupdate/check", {
            "hotVersion": "4", "apkVersion": "23", "deviceId": "device_123",
        })
        self.assertEqual(status, 200)
        self.assertEqual(rpc.calls, [("hotupdate_check", {"p_hot_version": 4, "p_apk_version": 23})])
        self.assertEqual(body["pkg"]["url"], "https://tv.example/v1/storages/object/hotupdate/backend-hu21.zip")

    def test_hotupdate_check_cache_coalesces_reads_and_returns_copies(self):
        api, rpc = self.make_api({"hotupdate_check": {"ret": 0, "pkg": {"objectKey": "a.zip"}}})
        request = {"hotVersion": 3, "apkVersion": 23, "deviceId": "device_123"}
        first = api.handle("POST", "/hotupdate/check", request)
        first[1]["pkg"]["objectKey"] = "changed.zip"
        second = api.handle("POST", "/hotupdate/check", request)
        self.assertEqual(second[1]["pkg"]["objectKey"], "a.zip")
        self.assertEqual(len(rpc.calls), 1)

    def test_hotupdate_blackout_skips_database_rpc(self):
        api, rpc = self.make_api(hotupdate_off=True)
        _, body, _headers = api.handle("POST", "/hotupdate/check", {
            "hotVersion": 1, "apkVersion": 23, "deviceId": "device_123",
        })
        self.assertEqual(body, {"ret": 0, "hasUpdate": False, "blackout": True})
        self.assertEqual(rpc.calls, [])

    def test_hotupdate_report_clips_untrusted_text_before_rpc(self):
        api, rpc = self.make_api()
        api.handle("POST", "/hotupdate/report", {
            "deviceId": "device_123", "result": "x" * 40, "detail": "y" * 600,
        })
        self.assertEqual(rpc.calls, [("hotupdate_report", {
            "p_device": "device_123", "p_apk_version": None, "p_from_version": None,
            "p_to_version": None, "p_result": "x" * 32, "p_detail": "y" * 500,
        })])

    def test_hotupdate_download_object_keys_cannot_escape_storage_root(self):
        api, rpc = self.make_api({"hotupdate_check": {
            "ret": 0, "hasUpdate": True, "pkg": {"objectKey": "../../private.zip"},
        }})
        status, body, _headers = api.handle("POST", "/hotupdate/check", {
            "hotVersion": 1, "apkVersion": 23, "deviceId": "device_123",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ret": 502, "msg": "upstream_error"})
        self.assertEqual(len(rpc.calls), 1)

    def test_health_is_available_and_retired_product_routes_are_closed(self):
        api, rpc = self.make_api()
        self.assertEqual(api.handle("GET", "/health")[1], {"status": "ok"})
        for path, method in (
            ("/activate", "POST"), ("/hotupdate/announce", "GET"),
            ("/hotupdate/vod-sources/check", "POST"), ("/internal/vod-sources", "GET"),
            ("/iptv/latest", "GET"), ("/iptv/refresh", "POST"),
        ):
            with self.subTest(path=path):
                status, body, _headers = api.handle(method, path, {"deviceId": "device_123"})
                self.assertEqual(status, 404)
                self.assertEqual(body, {"ret": 404, "msg": "not_found"})
        self.assertEqual(rpc.calls, [])

    def test_bad_device_and_apk_versions_are_rejected_without_rpc(self):
        api, rpc = self.make_api()
        self.assertEqual(api.handle("POST", "/check", {"deviceId": "short"})[1],
                         {"ret": 400, "msg": "bad_device"})
        self.assertEqual(api.handle("POST", "/check", {
            "deviceId": "device_123", "apkVersion": 0,
        })[1], {"ret": 400, "msg": "bad_apk"})
        self.assertEqual(rpc.calls, [])

    def test_rpc_and_public_origin_remain_private_and_https_only(self):
        with self.assertRaisesRegex(ValueError, "private self-hosted endpoint"):
            PostgrestRpcClient("https://appletv.api.tcloudbasegateway.com/v1/rdb/rest/rpc")
        with self.assertRaisesRegex(ValueError, "HTTPS origin"):
            BusinessApi(FakeRpc(), public_base_url="http://new-server.example")
        with self.assertRaisesRegex(ValueError, "CloudBase URLs"):
            BusinessApi(FakeRpc(), public_base_url="https://example.tcloudbase.com")

    def test_unknown_routes_remain_not_found_and_options_is_cors_safe(self):
        api, _rpc = self.make_api()
        self.assertEqual(api.handle("GET", "/unlisted")[0], 404)
        status, _body, headers = api.handle("OPTIONS", "/activate")
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Methods"], "GET, POST, OPTIONS")


if __name__ == "__main__":
    unittest.main()

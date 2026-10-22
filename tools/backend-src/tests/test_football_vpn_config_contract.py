import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.selfhosted.api_service import BusinessApi, RPC_ALLOWLIST  # noqa: E402


class FakeRpc:
    def __init__(self):
        self.calls = []

    def __call__(self, name, payload):
        self.calls.append((name, payload))
        return {}


class FootballVpnConfigContractTests(unittest.TestCase):
    def test_retired_public_config_route_is_not_found_without_rpc_access(self):
        rpc = FakeRpc()
        api = BusinessApi(rpc)

        for method in ("GET", "POST"):
            with self.subTest(method=method):
                request_body = {"deviceId": "test-device-1234"} if method == "POST" else {}
                status, body, _headers = api.handle(method, "/hotupdate/football-vpn/config", request_body)
                self.assertEqual(404, status)
                self.assertEqual({"ret": 404, "msg": "not_found"}, body)

        self.assertEqual([], rpc.calls)
        self.assertNotIn("football_vpn_config_get", RPC_ALLOWLIST)

    def test_only_hotupdate_rpcs_remain_public(self):
        sql = (ROOT / "tools" / "selfhosted" / "rpc_grants_repair.sql").read_text(encoding="utf-8")
        lower = sql.lower()

        self.assertIn("revoke all privileges on all functions in schema public", lower)
        self.assertIn("grant execute on function public.hotupdate_check", lower)
        self.assertIn("grant execute on function public.hotupdate_report", lower)
        for retired_rpc in (
            "activate_code", "announcement_latest", "vod_source_public_check",
            "football_vpn_config_get", "football_vpn_config_save", "admin_set_status",
        ):
            self.assertNotIn(f"grant execute on function public.{retired_rpc}", lower)
        self.assertEqual(lower.count("grant execute on function public."), 2)

        psql = (ROOT / "tools" / "selfhosted" / "apply_schema_migrations.psql").read_text(encoding="utf-8")
        runner = (ROOT / "tools" / "selfhosted" / "apply_schema_migrations.sh").read_text(encoding="utf-8")
        self.assertIn(r"\ir rpc_grants_repair.sql", psql)
        self.assertIn("selfhosted/rpc_grants_repair.sql", runner)


if __name__ == "__main__":
    unittest.main()

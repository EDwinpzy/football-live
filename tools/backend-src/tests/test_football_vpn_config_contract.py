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
                # The public API keeps a JSON protocol with HTTP 200; the retired route is
                # represented by its application-level ret=404 response.
                self.assertEqual(200, status)
                self.assertEqual({"ret": 404, "msg": "not_found"}, body)

        self.assertEqual([], rpc.calls)
        self.assertNotIn("football_vpn_config_get", RPC_ALLOWLIST)

    def test_existing_football_rpc_access_is_revoked_without_recreation_or_data_deletion(self):
        sql = (ROOT / "tools" / "selfhosted" / "rpc_grants_repair.sql").read_text(encoding="utf-8")
        lower = sql.lower()

        self.assertIn("do $retire_football_proxy$", lower)
        self.assertIn("to_regprocedure('public.football_vpn_config_get()') is not null", lower)
        self.assertIn("to_regprocedure('public.football_vpn_config_save(jsonb)') is not null", lower)
        self.assertIn("revoke all on function public.football_vpn_config_get() from public, anon, authenticated", lower)
        self.assertIn("revoke all on function public.football_vpn_config_save(jsonb) from public, anon, authenticated", lower)
        self.assertNotIn("create or replace function public.football_vpn_config_get()", lower)
        self.assertNotIn("create or replace function public.football_vpn_config_save", lower)
        self.assertNotIn("grant execute on function public.football_vpn_config_get()", lower)
        self.assertNotIn("grant execute on function public.football_vpn_config_save", lower)
        self.assertNotIn("drop function public.football_vpn_config_", lower)
        self.assertNotIn("delete from public.iptv_config", lower)

        psql = (ROOT / "tools" / "selfhosted" / "apply_schema_migrations.psql").read_text(encoding="utf-8")
        runner = (ROOT / "tools" / "selfhosted" / "apply_schema_migrations.sh").read_text(encoding="utf-8")
        self.assertIn(r"\ir rpc_grants_repair.sql", psql)
        self.assertIn("selfhosted/rpc_grants_repair.sql", runner)


if __name__ == "__main__":
    unittest.main()

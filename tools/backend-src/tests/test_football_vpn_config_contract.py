import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.selfhosted.api_service import BusinessApi, RPC_ALLOWLIST  # noqa: E402


class FakeRpc:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, name, payload):
        self.calls.append((name, payload))
        return self.result


class FootballVpnConfigContractTests(unittest.TestCase):
    def test_public_hotupdate_route_reads_the_database_rpc_without_cache(self):
        config = {
            "ret": 0,
            "version": 7,
            "updatedAt": "2026-10-11T01:02:03+00:00",
            "subscriptions": ["https://one.example/sub", "https://two.example/sub", "https://three.example/sub"],
        }
        rpc = FakeRpc(config)
        api = BusinessApi(rpc)

        status, body, headers = api.handle("GET", "/hotupdate/football-vpn/config")

        self.assertEqual(status, 200)
        self.assertEqual(body, config)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(rpc.calls, [("football_vpn_config_get", {})])
        self.assertIn("football_vpn_config_get", RPC_ALLOWLIST)

    def test_public_config_route_rejects_other_methods_and_malformed_rpc_data(self):
        rpc = FakeRpc({"ret": 0})
        api = BusinessApi(rpc)

        self.assertEqual(api.handle("POST", "/hotupdate/football-vpn/config", {})[1],
                         {"ret": 404, "msg": "not_found"})
        self.assertEqual(rpc.calls, [])
        status, body, headers = api.handle("GET", "/hotupdate/football-vpn/config")
        self.assertEqual((status, body), (200, {"ret": 502, "msg": "upstream_error"}))
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_config_rpcs_are_defined_secured_granted_and_repaired_for_existing_databases(self):
        sql = (ROOT / "tools" / "selfhosted" / "rpc_grants_repair.sql").read_text(encoding="utf-8")
        lower = sql.lower()
        for fragment in (
            "create or replace function public.football_vpn_config_get()",
            "create or replace function public.football_vpn_config_save(p_subscriptions jsonb)",
            "auth.uid()",
            "'2094843486709620737'",
            "grant execute on function public.football_vpn_config_get() to anon, authenticated",
            "grant execute on function public.football_vpn_config_save(jsonb) to authenticated",
            "revoke all on function public.football_vpn_config_save(jsonb) from public, anon, authenticated",
            "jsonb_array_length(p_subscriptions) <> 3",
            "!~* '^https://",
            "statement_timestamp()",
            "'version', 0",
            "'updatedat', null",
            "'version', 1",
            "greatest(coalesce((current_config.value::jsonb ->> 'version')::integer, 0), 0) + 1",
        ):
            self.assertIn(fragment.lower(), lower)

        config = (ROOT / "android" / "vpn-core" / "src" / "main" / "assets" / "football-mihomo.yaml")
        yaml = config.read_text(encoding="utf-8")
        for url in (
            "https://dasho.xn--cp3a08l.com/api/v1/pq/894be660f6a6e76a6b4c08f7c4f69e27",
            "https://yfjc.xyz/api/v1/client/subscribe?token=49cf2f9976db55702c28da2ce3e1b284",
            "https://api.fwcloud.xyz/link/LfmIxiV4oGAXtdG2?clash=1",
        ):
            self.assertIn(url, yaml)
            self.assertIn(url, sql)

        psql = (ROOT / "tools" / "selfhosted" / "apply_schema_migrations.psql").read_text(encoding="utf-8")
        runner = (ROOT / "tools" / "selfhosted" / "apply_schema_migrations.sh").read_text(encoding="utf-8")
        self.assertIn(r"\ir rpc_grants_repair.sql", psql)
        self.assertIn("selfhosted/rpc_grants_repair.sql", runner)


if __name__ == "__main__":
    unittest.main()

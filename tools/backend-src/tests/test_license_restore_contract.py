"""Same-device license restore contracts for Android and the self-hosted API/SQL."""
import pathlib
import unittest

from tools.selfhosted.api_service import BusinessApi

ROOT = pathlib.Path(__file__).resolve().parents[3]
TV = ROOT / "android/app/src/main/java/com/qiubo/optimaltv/license/LicenseManager.kt"
MOBILE = ROOT / "internal/mobile/app/src/main/java/com/qiubo/optimaltv/license/LicenseManager.kt"
# Restore lives in its own migration. A database predating the ledger already
# has the v2 activate_code signature, so keeping restore inside
# license_v2_migration.sql would let the migrator record it as applied without
# ever executing the branch.
API_SQL = ROOT / "tools/selfhosted/migrations/license_restore_v1.sql"
LEGACY_V2_SQL = ROOT / "tools/license_v2_migration.sql"


class LicenseRestoreContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TV.read_text(encoding="utf-8")
        cls.mobile = MOBILE.read_text(encoding="utf-8")
        cls.api_sql = API_SQL.read_text(encoding="utf-8").lower()

    def test_client_restores_only_when_not_activated(self):
        self.assertIn("private suspend fun tryRestore()", self.client)
        start = self.client.index("if (_state.value is LicenseState.NotActivated) {")
        block = self.client[start:start + 220]
        self.assertIn("tryRestore()", block)

    def test_both_clients_send_restore_without_code_and_reuse_ticket_validation(self):
        for source in (self.client, self.mobile):
            start = source.index("private suspend fun tryRestore()")
            block = source[start:source.index("private suspend fun applySuccess")]
            self.assertIn('callEndpoint("restore", "", deviceId(), null)', block)
            self.assertIn("applySuccess(resp)", block)

    def test_selfhosted_api_accepts_empty_restore_code_and_forwards_device(self):
        calls = []

        def rpc(name, payload):
            calls.append((name, payload))
            return {"ret": 404, "msg": "restore_not_found"}

        api = BusinessApi(rpc)
        status, body, _headers = api.handle(
            "POST", "/activate",
            {"protocol": 2, "action": "restore", "code": "", "deviceId": "a" * 64},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body, {"ret": 404, "msg": "restore_not_found"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "activate_code")
        self.assertEqual(calls[0][1]["p_action"], "restore")
        self.assertIsNone(calls[0][1]["p_code"], "restore must not read or look up an empty card code")
        self.assertEqual(calls[0][1]["p_device"], "a" * 64)

    def test_sql_restore_selects_latest_current_valid_bound_nonbanned_authorization(self):
        sql = self.api_sql
        self.assertIn("'restore'", sql)
        start = sql.index("if v_action = 'restore'")
        end = sql.index("if v_code !~", start)
        branch = sql[start:end]

        for requirement in (
            "coalesce(l.devices", "? v_device", "activated_at is not null", "status = 'active'",
            "expire_at is null or", "expire_at > v_now", "order by l.activated_at desc",
            "restore_ok", "restore_not_found", "'ticket'", "'licensecode'",
            "'activatedat'", "'expireat'", "'servernow'",
        ):
            self.assertIn(requirement, branch)
        self.assertNotIn("update public.licenses", branch)
        self.assertNotIn("devices :=", branch)
        self.assertNotIn("expire_at =", branch)
        self.assertNotIn("activated_at =", branch)

    def test_restore_is_not_carried_only_by_license_v2_migration(self):
        legacy = LEGACY_V2_SQL.read_text(encoding="utf-8").lower()
        self.assertNotIn(
            "v_action = 'restore'", legacy,
            "restore must live in its own migration; a legacy database already has the v2 "
            "activate_code signature and would skip a restore hidden inside this file",
        )
        # The marker is what the migrator probes in the installed function body.
        # self.api_sql is already lowercased in setUpClass.
        self.assertIn("otv-restore-marker-begin", self.api_sql.lower())

    def test_both_app_trees_identical(self):
        self.assertEqual(self.client, self.mobile, "两工程不一致，请跑 tools/sync_shared.py")


if __name__ == "__main__":
    unittest.main()

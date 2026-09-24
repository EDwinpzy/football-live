import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "license_v2_migration.sql"
RESET = ROOT / "license_v2_reset.py"
RESET_SQL = ROOT / "license_v2_reset.sql"
PROJECT_ROOT = ROOT.parent


class LicenseV2SqlContractTest(unittest.TestCase):
    def test_schema_migration_is_idempotent_and_non_destructive(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        for column in ("activated_at", "expire_at", "redeemed_at", "redeemed_to"):
            self.assertIn(f"add column if not exists {column}", sql)
        self.assertIn("idx_licenses_expire_at", sql)
        self.assertIn("idx_licenses_redeemed_to", sql)
        self.assertNotIn("truncate", sql)
        self.assertNotIn("delete from licenses", sql)
        self.assertNotIn("delete from public.licenses", sql)

    def test_activate_rpc_has_protocol_actions_and_row_locks(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        for parameter in ("p_protocol integer", "p_action text", "p_current_code text"):
            self.assertIn(parameter, sql)
        for action in ("'activate'", "'verify'", "'renew'"):
            self.assertIn(action, sql)
        self.assertIn("for update", sql)
        self.assertIn("426", sql)

    def test_verify_does_not_append_a_new_device(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        verify_start = sql.index("if v_action = 'verify'")
        verify_end = sql.index("elsif v_action = 'renew'", verify_start)
        verify_branch = sql[verify_start:verify_end]
        self.assertIn("406", verify_branch)
        self.assertNotIn("devices :=", verify_branch)
        self.assertNotIn("jsonb_build_array", verify_branch)

    def test_renewal_extends_canonical_card_and_marks_redeemed_card(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        self.assertIn("redeemed_at = v_now", sql)
        self.assertIn("redeemed_to = v_current_code", sql)
        self.assertIn("greatest(v_current.expire_at, v_now)", sql)
        self.assertIn("order by code for update", sql)

    def test_admin_projection_includes_unified_expiry_fields(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        for field in ("activated_at", "expire_at", "redeemed_at", "redeemed_to"):
            self.assertGreaterEqual(sql.count(field), 3)
        self.assertIn("p_status = 'redeemed'", sql)


class LicenseV2ResetContractTest(unittest.TestCase):
    def test_reset_requires_environment_snapshot_and_confirmation(self):
        src = RESET.read_text(encoding="utf-8")
        self.assertIn("CLEAR-ALL-LICENSES", src)
        self.assertIn('"mytv-selfhosted"', src)
        self.assertNotIn("appletv-d5ge1bth794873f76", src)
        self.assertIn('TOOLS / "selfhosted" / "import_licenses.py"', src)
        self.assertNotIn("tools/db_import.py", src)
        self.assertIn("--snapshot", src)
        self.assertIn("snapshot", src)
        self.assertIn("regenerate", src)

    def test_reset_writes_backup_before_clear(self):
        src = RESET.read_text(encoding="utf-8")
        reset_sql = RESET_SQL.read_text(encoding="utf-8").lower()
        self.assertIn("sha256", src.lower())
        self.assertIn("planCounts", src)
        self.assertIn("admin_license_v2_clear", src)
        self.assertIn("clear-all-licenses", reset_sql)
        self.assertIn("delete from public.activate_log", reset_sql)
        self.assertIn("delete from public.licenses", reset_sql)


class LicenseV2AndroidContractTest(unittest.TestCase):
    def test_both_apps_reverify_when_returning_to_foreground(self):
        paths = (
            PROJECT_ROOT / "android/app/src/main/java/com/qiubo/optimaltv/MainActivity.kt",
            PROJECT_ROOT / "internal/mobile/app/src/main/java/com/qiubo/optimaltv/MainActivity.kt",
        )
        for path in paths:
            source = path.read_text(encoding="utf-8")
            resume = source[source.index("override fun onResume()") : source.index("override fun onPause()")]
            self.assertIn("LicenseManager.requestReverify()", resume, path.as_posix())


if __name__ == "__main__":
    unittest.main()

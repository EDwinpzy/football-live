import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "tools" / "vod_sources_migration.sql"
BUSINESS_API = ROOT / "tools" / "selfhosted" / "api_service.py"


class VodSourcePublishContractTest(unittest.TestCase):
    def test_migration_exposes_required_rpc_without_table_grants(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        for name in ("vod_source_admin_list", "vod_source_admin_save", "vod_source_admin_delete",
                     "vod_source_admin_publish", "vod_source_public_check"):
            self.assertIn("function " + name, sql)
        self.assertNotIn("grant select on vod_source_drafts to anon", sql)

    def test_publish_is_versioned_and_atomic(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        self.assertIn("for update", sql)
        self.assertIn("vod_source_versions", sql)
        self.assertIn("sha256", sql)
        self.assertIn("at least one enabled source", sql)

    def test_selfhosted_hotupdate_api_exposes_vod_source_check(self):
        api = BUSINESS_API.read_text(encoding="utf-8")
        self.assertIn('"/vod-sources/check"', api)
        self.assertIn('"vod_source_public_check"', api)
        self.assertIn("VOD_SOURCE_CACHE_SECONDS = 120", api)


if __name__ == "__main__":
    unittest.main()

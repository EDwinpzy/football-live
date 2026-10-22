import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class CloudCuContractTest(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_hotupdate_function_has_a_bounded_rpc_cache(self):
        api = self.read("tools/selfhosted/api_service.py")
        self.assertIn("CHECK_CACHE_SECONDS = 120", api)
        self.assertIn('"hotupdate_check"', api)

    def test_hotupdate_applied_report_is_transition_only(self):
        for path in (
            "android/app/src/main/java/com/qiubo/optimaltv/hotupdate/HotUpdateManager.kt",
            "internal/mobile/app/src/main/java/com/qiubo/optimaltv/hotupdate/HotUpdateManager.kt",
        ):
            src = self.read(path)
            self.assertIn('active > 0 && active != prevActive', src)

    def test_database_indexes_are_recorded_as_idempotent_migration(self):
        sql = self.read("tools/pg_cu_optimization_migration.sql")
        self.assertIn("create index if not exists idx_hotupdate_pkg_status_version", sql.lower())


if __name__ == "__main__":
    unittest.main()

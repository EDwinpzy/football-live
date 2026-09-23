import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class CloudCuContractTest(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_hotupdate_function_coalesces_postgres_reads(self):
        api = self.read("tools/selfhosted/api_service.py")
        self.assertIn("ANNOUNCE_CACHE_SECONDS = 300", api)
        self.assertIn("CHECK_CACHE_SECONDS = 120", api)
        self.assertIn('"announcement_latest"', api)
        self.assertIn('"hotupdate_check"', api)
        self.assertIn("stale-while-revalidate=300", api)

    def test_native_announcement_polling_is_foreground_only(self):
        for path in (
            "android/app/src/main/java/com/qiubo/optimaltv/announcement/AnnouncementManager.kt",
            "internal/mobile/app/src/main/java/com/qiubo/optimaltv/announcement/AnnouncementManager.kt",
        ):
            src = self.read(path)
            self.assertIn("POLL_MS = 5 * 60_000L", src)
            self.assertIn("if (!foreground) return", src)
            self.assertIn("pollInFlight.compareAndSet(false, true)", src)

    def test_hotupdate_applied_report_is_transition_only(self):
        for path in (
            "android/app/src/main/java/com/qiubo/optimaltv/hotupdate/HotUpdateManager.kt",
            "internal/mobile/app/src/main/java/com/qiubo/optimaltv/hotupdate/HotUpdateManager.kt",
        ):
            src = self.read(path)
            self.assertIn('active > 0 && active != prevActive', src)

    def test_license_reverification_is_throttled(self):
        web = self.read("tools/backend-src/www/app.js")
        self.assertIn("LICENSE_REVERIFY_MS = 6 * 60 * 60 * 1000", web)
        for path in (
            "android/app/src/main/java/com/qiubo/optimaltv/license/LicenseManager.kt",
            "internal/mobile/app/src/main/java/com/qiubo/optimaltv/license/LicenseManager.kt",
        ):
            self.assertIn("REVERIFY_INTERVAL_MS = 6 * 3600_000L", self.read(path))

    def test_database_indexes_are_recorded_as_idempotent_migration(self):
        sql = self.read("tools/pg_cu_optimization_migration.sql")
        for index in (
            "idx_announcements_status_id",
            "idx_activate_log_at",
            "idx_licenses_updated_at",
            "idx_licenses_created_at",
            "idx_hotupdate_pkg_status_version",
        ):
            self.assertIn("create index if not exists " + index, sql.lower())


if __name__ == "__main__":
    unittest.main()

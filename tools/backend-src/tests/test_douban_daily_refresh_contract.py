import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
ANDROID = ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv"
MOBILE = ROOT / "internal" / "mobile" / "app" / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv"
APP = ROOT / "tools" / "backend-src" / "www" / "app.js"


class DoubanDailyRefreshContractTest(unittest.TestCase):
    def test_android_preserves_home_stale_marker(self):
        for root in (ANDROID, MOBILE):
            source = (root / "data" / "source" / "HhkanSource.kt").read_text(encoding="utf-8")
            self.assertIn("stale: Boolean", source)
            self.assertIn("source: String", source)

    def test_android_retries_stale_home_until_daily_refresh_arrives(self):
        for root in (ANDROID, MOBILE):
            source = (root / "data" / "repo" / "VodRepository.kt").read_text(encoding="utf-8")
            # The dedicated refreshStaleHome poller was replaced by the local
            # metadata catalogue: empty library blocks are now retried on a
            # backoff schedule until real content arrives, and the startup gate
            # waits for content rather than for a job to merely finish. Assert
            # that behaviour survives instead of the retired symbol.
            self.assertIn("scheduleLibRetry", source)
            self.assertIn("LIB_RETRY_DELAYS", source)
            self.assertIn("awaitLibBlocksContent", source)

    def test_web_retries_stale_home_until_daily_refresh_arrives(self):
        source = APP.read_text(encoding="utf-8")
        self.assertIn("doubanHome.stale", source)
        self.assertIn("refreshStaleVodHome", source)


if __name__ == "__main__":
    unittest.main()

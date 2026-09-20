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
            self.assertIn("home.stale", source)
            self.assertIn("refreshStaleHome", source)

    def test_web_retries_stale_home_until_daily_refresh_arrives(self):
        source = APP.read_text(encoding="utf-8")
        self.assertIn("doubanHome.stale", source)
        self.assertIn("refreshStaleVodHome", source)


if __name__ == "__main__":
    unittest.main()

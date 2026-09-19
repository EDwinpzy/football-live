import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
APPS = (ROOT / "android" / "app", ROOT / "internal" / "mobile" / "app")


class AndroidLifecycleContractTest(unittest.TestCase):
    def test_both_apps_use_non_sticky_backend_and_explicit_exit(self):
        for app in APPS:
            main = app / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv"
            backend = (main / "BackendService.kt").read_text(encoding="utf-8")
            hint = (main / "ui" / "components" / "Hint.kt").read_text(encoding="utf-8")
            self.assertIn("ACTION_SHUTDOWN", backend)
            self.assertIn("return START_NOT_STICKY", backend)
            self.assertNotIn("return START_STICKY", backend)
            self.assertIn("ExitCoordinator.exit", hint)

    def test_both_apps_signal_foreground_network_refresh(self):
        for app in APPS:
            main = app / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv"
            application = (main / "App.kt").read_text(encoding="utf-8")
            activity = (main / "MainActivity.kt").read_text(encoding="utf-8")
            live = (main / "ui" / "live" / "LiveScreen.kt").read_text(encoding="utf-8")
            self.assertIn("AppLifecycleManager.init", application)
            self.assertIn("AppLifecycleManager.onForeground", activity)
            self.assertIn("AppLifecycleManager.onBackground", activity)
            self.assertIn("refreshEpoch", live)

    def test_both_apps_retry_backend_when_activity_returns_foreground(self):
        for app in APPS:
            main = app / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv"
            activity = (main / "MainActivity.kt").read_text(encoding="utf-8")
            resume = activity[activity.index("override fun onResume()"):activity.index("override fun onPause()")]
            self.assertIn("BackendService.ensure(this)", resume)

    def test_both_players_renew_live_after_long_background(self):
        for app in APPS:
            player = app / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv" / "ui" / "player" / "PlayerViewModel.kt"
            source = player.read_text(encoding="utf-8")
            self.assertIn("hostPausedAtMs", source)
            self.assertIn("AppLifecyclePolicy.onForeground", source)
            self.assertIn("REFRESH_AND_RENEW_LIVE", source)


if __name__ == "__main__":
    unittest.main()

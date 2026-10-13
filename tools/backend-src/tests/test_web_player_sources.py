import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
HARNESS = ROOT / "tools" / "backend-src" / "tests" / "web_player_sources_harness.mjs"
HTML = ROOT / "tools" / "backend-src" / "www" / "index.html"
CSS = ROOT / "tools" / "backend-src" / "www" / "style.css"


class WebPlayerSourcesTest(unittest.TestCase):
    def test_player_source_browser_harness(self):
        result = subprocess.run(
            ["node", str(HARNESS)],
            cwd=ROOT / "tools" / "backend-src",
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertIn("All player-source harness checks passed.", result.stdout)

    def test_landscape_source_panel_has_a_reopenable_error_entry(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('id="mpToggle"', html)
        self.assertIn('id="mpSourceState"', html)
        self.assertIn('id="mpSourceRetry"', html)
        self.assertIn('id="mpClose"', html)
        self.assertIn('id="mpLineProviders" role="group" aria-label="信号源分组"', html)
        self.assertIn('class="mp-route-list" id="mpLines"', html)
        self.assertNotIn("mpLinesToggle", html, "the old route disclosure arrow is removed")

    def test_source_rows_use_a_fixed_height_independently_scrollable_list(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertRegex(css, r"\.mp-route-list\{[^}]*height:192px;min-height:192px;max-height:192px")
        self.assertRegex(css, r"\.mp-route-list\{[^}]*overflow-x:hidden;overflow-y:auto")
        self.assertIn("overscroll-behavior-y:contain", css)

    def test_vod_source_list_follows_episodes_and_speed_in_the_same_panel(self):
        html = HTML.read_text(encoding="utf-8")
        positions = [
            html.index('id="mpEpsWrap"'),
            html.index('id="mpSpeedSec"'),
            html.index('id="mpLinesSec"'),
            html.index('id="mpAspect"'),
        ]
        self.assertEqual(positions, sorted(positions),
                         "the VOD panel order is episodes, speed, source list, then aspect")


if __name__ == "__main__":
    unittest.main()

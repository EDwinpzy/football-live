import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
HARNESS = ROOT / "tools" / "backend-src" / "tests" / "web_player_sources_harness.mjs"
HTML = ROOT / "tools" / "backend-src" / "www" / "index.html"


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
        self.assertIn('class="hscroll mp-chips mp-routes" id="mpLines"', html)
        self.assertIn('id="mpLinesToggle" class="mp-route-toggle"', html)


if __name__ == "__main__":
    unittest.main()

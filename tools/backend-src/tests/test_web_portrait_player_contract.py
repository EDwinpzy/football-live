import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WWW = ROOT / "tools" / "backend-src" / "www"


class WebPortraitPlayerContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WWW / "index.html").read_text(encoding="utf-8")
        cls.app = (WWW / "app.js").read_text(encoding="utf-8")
        cls.css = (WWW / "style.css").read_text(encoding="utf-8")

    def test_more_button_is_removed_from_player(self):
        self.assertNotIn('id="moreBtn"', self.html)
        self.assertNotIn("#moreBtn", self.app)
        self.assertNotIn("#moreBtn", self.css)

    def test_portrait_controls_overlay_the_video_region(self):
        self.assertIn("body.portrait #controls{display:flex;top:0;left:0;right:0;height:calc(100vw * 9 / 16);bottom:auto}", self.css)
        self.assertIn("body.portrait .ctl-title,body.portrait .ctl-meta,body.portrait .sp26{display:none}", self.css)

    def test_fullscreen_button_stays_in_video_control_row(self):
        row_start = self.html.index('<div class="ctl-progress-row">')
        row_end = self.html.index("</div>", row_start)
        self.assertIn('id="ctlFull"', self.html[row_start:row_end])

    def test_portrait_open_shows_the_video_controls(self):
        portrait_start = self.app.index("if (state.portrait) {", self.app.index("function openPlayer"))
        portrait_end = self.app.index("\n  }", portrait_start)
        self.assertIn("showControls();", self.app[portrait_start:portrait_end])


if __name__ == "__main__":
    unittest.main()

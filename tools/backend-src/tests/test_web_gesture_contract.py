"""网页版边缘返回手势契约。

边缘右滑必须走统一的应用内返回语义，且不能把播放器横滑、鼠标拖动或
多指触控误判成返回。
"""
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]


class WebGestureContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "tools/backend-src/www/app.js").read_text(encoding="utf-8")

    def test_edge_back_uses_pointer_events_and_shared_back_handler(self):
        self.assertIn("const EDGE_BACK_ZONE = 44", self.source)
        self.assertIn("const EDGE_BACK_DISTANCE = 64", self.source)
        self.assertIn('addEventListener("pointerdown"', self.source)
        self.assertIn('addEventListener("pointerup"', self.source)
        self.assertIn('addEventListener("pointercancel"', self.source)
        self.assertIn("pointerType === \"mouse\"", self.source)
        self.assertIn("uiBack()", self.source)

    def test_edge_back_guards_primary_pointer_and_player_horizontal_swipes(self):
        self.assertIn("!e.isPrimary", self.source)
        self.assertIn('target.closest("#video, #cTrack, .ctl-track, input, textarea")', self.source)
        self.assertIn("Math.abs(endY - startY) < EDGE_BACK_VERTICAL_TOLERANCE", self.source)

    def test_touch_fallback_is_kept_for_webviews_without_pointer_events(self):
        self.assertIn('if ("PointerEvent" in window)', self.source)
        self.assertIn('addEventListener("touchstart"', self.source)
        self.assertIn('addEventListener("touchend"', self.source)


if __name__ == "__main__":
    unittest.main()

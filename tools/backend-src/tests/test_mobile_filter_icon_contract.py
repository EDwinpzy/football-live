# -*- coding: utf-8 -*-
"""移动片库筛选入口契约（右上角图标化 redesign）。"""

import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
ALL_SCREEN = (
    ROOT
    / "internal"
    / "mobile"
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "qiubo"
    / "optimaltv"
    / "ui"
    / "all"
    / "AllScreen.kt"
)
BACK_BUTTON = (
    ROOT
    / "internal"
    / "mobile"
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "qiubo"
    / "optimaltv"
    / "ui"
    / "components"
    / "BackButton.kt"
)


class MobileFilterIconContractTest(unittest.TestCase):
    def setUp(self):
        self.source = ALL_SCREEN.read_text(encoding="utf-8")
        self.back_button_source = BACK_BUTTON.read_text(encoding="utf-8")

    def test_toggle_is_an_icon_button_with_accessible_filter_state(self):
        self.assertIn("private fun FilterToggle(", self.source)
        self.assertIn("Canvas(", self.source)
        self.assertIn("testTag(MobileUiTags.FilterToggle)", self.source)
        self.assertIn(
            'contentDescription = if (active) "筛选（已选 $activeCount）" else "筛选"',
            self.source,
        )
        self.assertNotIn('Text("⌄"', self.source)

    def test_toggle_keeps_touch_target_and_active_badge(self):
        self.assertIn(".size(MobileBackButtonTouchTarget)", self.source)
        self.assertIn("val active = activeCount > 0", self.source)
        self.assertIn("CircleShape", self.source)

    def test_filter_panel_reserves_a_left_safe_lane_for_back_button(self):
        self.assertIn("val filterPanelInset = 36f", self.source)
        self.assertIn("val filterSafeRailWidth = sidePad + filterPanelInset", self.source)
        self.assertIn(".width(filterSafeRailWidth.sx(s))", self.source)
        self.assertIn(".height(MobileBackButtonInset + MobileBackButtonTouchTarget)", self.source)

    def test_movie_is_the_default_category(self):
        self.assertIn('var cat by remember { mutableStateOf("1") }', self.source)
        self.assertIn('ifBlank { "1" }', self.source)

    def test_header_alignment_removes_title_and_shares_back_button_baseline(self):
        self.assertNotIn('"片库筛选"', self.source)
        self.assertIn("val filterBackOffset = if (portrait) 8f else 16f", self.source)
        self.assertIn(".offset(x = filterBackOffset.sx(s))", self.source)
        self.assertIn(".offset(y = (-8f).sx(s))", self.source)

    def test_filter_icon_has_no_white_button_background(self):
        self.assertIn("val buttonBg = Color.Transparent", self.source)
        self.assertIn("val iconFg = OtvColors.White75", self.source)

    def test_header_background_is_not_cut_out_and_back_icon_is_smaller(self):
        self.assertIn(".background(Color.Transparent)", self.source)
        self.assertIn("iconSize = 22.dp", self.source)
        self.assertIn("iconSize: Dp = 26.dp", self.back_button_source)
        self.assertIn(".size(iconSize)", self.back_button_source)

    def test_filter_rows_do_not_draw_hairline_dividers(self):
        self.assertNotIn(".background(OtvColors.Line)", self.source)


if __name__ == "__main__":
    unittest.main()

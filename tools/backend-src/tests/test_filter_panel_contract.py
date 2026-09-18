# -*- coding: utf-8 -*-
"""片库筛选面板契约（2026-09-18 用户要求：返回钮与筛选面板顶部对齐 + 筛选组件可收起）。

这两条都是「看着差不多、其实很容易漂」的版式不变量，所以钉成契约：

1. 面板顶边必须等于悬浮返回钮的贴边量——网页端两端都写成 `12px/var(--z,1)`（漏除 --z
   竖屏会落在 6.2px，比返回钮还高半格）；移动端两端都取同一个常量 `MobileBackButtonInset`。
   历史：旧做法是把整个面板下推 64dp（网页端 `.all-body{padding-top:...}`）给返回钮让位，
   窄横屏上缝太大（用户 2026-09-18 报障「没对齐」）。
2. 收起态只隐藏筛选行、必须保留表头开关；有非默认筛选时开关要显示「已选 N」——
   否则用户收起面板后看不到自己选了什么。
"""

import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
WWW = ROOT / "tools" / "backend-src" / "www"
APP = WWW / "app.js"
CSS = WWW / "style.css"
MOBILE = ROOT / "internal" / "mobile" / "app" / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv"
BACK_BUTTON = MOBILE / "ui" / "components" / "BackButton.kt"
COMPONENTS = MOBILE / "ui" / "components" / "VodComponents.kt"
ALL_SCREEN = MOBILE / "ui" / "all" / "AllScreen.kt"

# 面板顶边与返回钮贴边必须同源的表达式（网页端）/ 常量（移动端）
WEB_TOP = "calc(12px/var(--z,1)"
MOBILE_INSET = "MobileBackButtonInset"


class WebFilterPanelContractTest(unittest.TestCase):
    def setUp(self):
        self.css = CSS.read_text(encoding="utf-8")
        self.app = APP.read_text(encoding="utf-8")

    def test_panel_top_matches_back_button_inset(self):
        cs = self.css.replace(" ", "")
        # 横屏与竖屏都必须除以 --z（返回钮自身也是 12px/--z，漏除就对不上）
        self.assertIn(
            ".filter-panel{margin:calc(12px/var(--z,1)+env(safe-area-inset-top))60px0;", cs
        )
        self.assertIn(
            "body.portrait.filter-panel{margin:calc(12px/var(--z,1)+env(safe-area-inset-top))40px0}",
            cs,
        )

    def test_old_push_down_hack_is_gone(self):
        """旧做法：把 .all-body 顶部留白补到 64 物理 px 给返回钮让位（缝太大，已废）。"""
        cs = self.css.replace(" ", "")
        self.assertNotIn("64px/var(--z,1)-36px", cs)
        self.assertIn("body.portrait.all-body{padding-top:0}", cs)

    def test_collapsed_hides_rows_and_keeps_header(self):
        cs = self.css.replace(" ", "")
        self.assertIn(".filter-panel.collapsed.fp-rows{display:none}", cs)
        self.assertIn(".fp-head{", cs)
        self.assertIn(".filter-panel.collapsed.fp-caret{transform:rotate(-180deg)}", cs)

    def test_panel_state_is_remembered(self):
        self.assertIn('filterOpen: store.get("otvw:filterOpen", true) !== false', self.app)
        self.assertIn('store.set("otvw:filterOpen", all.filterOpen)', self.app)
        self.assertIn('panel.className = "filter-panel" + (open ? "" : " collapsed")', self.app)

    def test_toggle_is_wired_and_signals_active_filters(self):
        self.assertIn('id="filterToggle"', self.app)
        self.assertIn("has-filter", self.app)
        # 表头与筛选行分容器：收起时只隐藏 .fp-rows，表头开关留着
        self.assertIn('<div class="fp-rows">', self.app)
        # 收起态仍有线索：非默认筛选时显示「已选 N」
        self.assertIn('筛选${activeCount ? "（已选 " + activeCount + "）" : ""}', self.app)


class MobileFilterPanelContractTest(unittest.TestCase):
    def setUp(self):
        self.back = BACK_BUTTON.read_text(encoding="utf-8")
        self.comp = COMPONENTS.read_text(encoding="utf-8")
        self.all_screen = ALL_SCREEN.read_text(encoding="utf-8")

    def test_back_button_exposes_alignment_constants(self):
        self.assertIn("val MobileBackButtonInset = 12.dp", self.back)
        self.assertIn("val MobileBackButtonTouchTarget = 44.dp", self.back)

    def test_panel_top_reuses_the_back_button_inset(self):
        self.assertIn("top = MobileBackButtonInset,", self.all_screen)

    def test_header_clears_the_back_button(self):
        """面板同顶后首行仍不能被返回钮压住：表头高度取返回钮触控命中区。"""
        self.assertIn(".height(MobileBackButtonTouchTarget),", self.all_screen)

    def test_filter_rows_are_gated_by_the_open_state(self):
        self.assertIn("var filterOpen by rememberSaveable { mutableStateOf(true) }", self.all_screen)
        self.assertIn("if (filterOpen) {", self.all_screen)
        # 六行筛选必须都在收起闸门之后
        gate = self.all_screen.index("if (filterOpen) {")
        for row in ("类别", "类型", "地区", "年份", "评分", "排序"):
            at = self.all_screen.index('VodFilterRow("%s"' % row)
            self.assertGreater(at, gate, row)

    def test_toggle_has_a_test_tag_and_active_hint(self):
        self.assertIn('const val FilterToggle = "mobile-vod-filter-toggle"', self.comp)
        self.assertIn("testTag(MobileUiTags.FilterToggle)", self.all_screen)
        self.assertIn('"筛选（已选 $activeCount）"', self.all_screen)

    def test_stale_clearance_constant_is_gone(self):
        hits = [p for p in (ROOT / "internal").rglob("*.kt")
                if "MobileBackButtonClearance" in p.read_text(encoding="utf-8")]
        hits += [p for p in (ROOT / "android").rglob("*.kt")
                 if "MobileBackButtonClearance" in p.read_text(encoding="utf-8")]
        self.assertEqual([], hits)


if __name__ == "__main__":
    unittest.main()

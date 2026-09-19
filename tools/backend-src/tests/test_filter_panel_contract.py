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


class RailStyleContractTest(unittest.TestCase):
    """「轨」样式契约（2026-09-19 设计稿 v2 方案①，老板拍板）：裸文字选项 + 选中白胶囊。

    钉死三端同源的关键样式锚点，防止后续美化改回「满屏小灰丸」：
    1. 未选 chip 无底色（白 55% 文字），选中 = 白底黑字 + 字重 700 + 全圆角；
    2. 行间 1px 白 5% 发丝分隔（首行不画）；
    3. TV 端焦点 = 白环不落底（focusedBg 仅当选中时才给白胶囊）。
    """

    def test_web_chips_are_bare_text_until_selected(self):
        cs = CSS.read_text(encoding="utf-8").replace(" ", "")
        # 未选：透明底 + 白 55% 字 + 全圆角 + hover/pressed 反馈
        self.assertIn("color:rgba(255,255,255,.55);background:transparent", cs)
        self.assertIn("border-radius:999px", cs)
        self.assertIn(".fp-row.fchip:hover{color:rgba(255,255,255,.85)}", cs)
        self.assertIn(".fp-row.fchip:active{transform:scale(.96)}", cs)
        # 选中：白底黑字加粗
        self.assertIn(".fp-row.fchip.on{background:#fff;color:#0E0E0F;font-weight:700}", cs)
        # 旧「灰丸」样式不得复活
        self.assertNotIn("color:rgba(255,255,255,.72);background:rgba(255,255,255,.12)", cs)

    def test_web_rows_have_hairline_separators(self):
        cs = CSS.read_text(encoding="utf-8").replace(" ", "")
        self.assertIn(".fp-row+.fp-row{border-top:1pxsolidrgba(255,255,255,.05)}", cs)
        self.assertIn(".fp-row .fp-label{width:52px;".replace(" ", ""), cs)
        # 面板底色降到白 5%
        self.assertIn("background:rgba(255,255,255,.05);border-radius:18px", cs)

    def test_mobile_and_tv_share_the_rail_style(self):
        comp = COMPONENTS.read_text(encoding="utf-8")
        # 移动版：全圆角 + 选中白/未选透明 + 200ms 颜色渐变
        self.assertIn("RoundedCornerShape(percent = 50)", comp)
        self.assertIn("animateColorAsState(", comp)
        self.assertIn("if (active) OtvColors.White else Color.Transparent", comp)
        self.assertIn("if (active) OtvColors.Bg else OtvColors.White55", comp)
        self.assertIn("if (active) FontWeight.Bold else FontWeight.Medium", comp)

        tv = (ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "qiubo" / "optimaltv"
              / "ui" / "all" / "AllScreen.kt").read_text(encoding="utf-8")
        # TV 版：焦点不再落白底（白环），仅选中 chip 才给白胶囊
        self.assertIn("focusedBg = if (active) OtvColors.White else null,", tv)
        self.assertIn("RoundedCornerShape(percent = 50)", tv)
        self.assertIn("if (active) FontWeight.Bold else FontWeight.Medium", tv)
        # 两端面板都用白 5% 底 + 发丝分隔
        self.assertIn("Color(0x0DFFFFFF)", tv)
        self.assertIn("Color(0x0DFFFFFF)", ALL_SCREEN.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

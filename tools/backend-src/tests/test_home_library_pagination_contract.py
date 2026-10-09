"""首页片库分页布局契约（TV / mobile Compose 与网页版）。

分页的产品口径是固定两行：TV 每页 14 张（7×2），移动端每页按列数×2，
网页版横屏 7×2、竖屏 3×2。每个分类返回的三栏都应独立走分页组件；切分类和
重新进入页面时复用短缓存，过期后仍要刷新，不得恢复旧的 TV libBlocks UI 数据通路。
"""

import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
ANDROID = ROOT / "android/app/src/main/java/com/qiubo/optimaltv"
MOBILE = ROOT / "internal/mobile/app/src/main/java/com/qiubo/optimaltv"
WEB_APP = ROOT / "tools/backend-src/www/app.js"
WEB_CSS = ROOT / "tools/backend-src/www/style.css"

EXPECTED_TV_SECTIONS = ("最近热门", "最新上线", "最近更新")
EXPECTED_WEB_SECTIONS = ("最近热门", "最新上映", "豆瓣高分")
EXPECTED_WEB_PAGED_CATEGORIES = {"movie", "tv", "anime", "variety", "short"}


def _source_block(source, start_marker, end_marker):
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def _css_rule(source, selector):
    start = source.index(selector)
    opening = source.index("{", start)
    depth = 0
    for pos in range(opening, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:pos]
    raise AssertionError(f"CSS 规则未闭合: {selector}")


class HomeLibraryPaginationContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tv_home = (ANDROID / "ui/home/HomeScreen.kt").read_text(encoding="utf-8")
        cls.mobile_home = (MOBILE / "ui/home/HomeScreen.kt").read_text(encoding="utf-8")
        cls.tv_repo = (ANDROID / "data/repo/VodRepository.kt").read_text(encoding="utf-8")
        cls.mobile_repo = (MOBILE / "data/repo/VodRepository.kt").read_text(encoding="utf-8")
        cls.web_app = WEB_APP.read_text(encoding="utf-8")
        cls.web_css = WEB_CSS.read_text(encoding="utf-8")

    def test_tv_page_is_fixed_two_rows_of_seven(self):
        pager = _source_block(
            self.tv_home,
            "private fun PagedLibSection(",
            "/** 片库竖卡",
        )
        self.assertIn("items.chunked(HomeLibraryUiPolicy.PAGE_SIZE)", pager, "TV 每页必须严格按 14 张分段")
        self.assertIn("repeat(2) { row ->", pager, "TV 分页必须始终绘制两行")
        self.assertIn("repeat(HomeLibraryUiPolicy.ROW_SIZE) { col ->", pager, "TV 每行必须有 7 个固定槽位")
        self.assertIn("row * HomeLibraryUiPolicy.ROW_SIZE + col", pager)
        self.assertNotIn('Text("上一页"', pager)
        self.assertNotIn('Text("下一页"', pager)
        self.assertIn("detectHorizontalDragGestures(", pager)
        self.assertIn("targetPageForDpad", pager)

    def test_mobile_page_is_two_rows_of_current_column_count(self):
        pager = _source_block(
            self.mobile_home,
            "private fun PagedMobileLibSection(",
            "/** 片库竖卡",
        )
        self.assertIn("items.chunked(cols * 2)", pager, "移动端每页应为列数×2 张")
        self.assertIn("HorizontalPager(state = pager", pager, "移动端应横向翻页")
        self.assertIn("repeat(2) { row ->", pager, "移动端分页必须始终绘制两行")
        self.assertIn("repeat(cols) { col ->", pager)
        self.assertIn('Text("${pager.currentPage + 1} / ${pages.size}"', pager)
        self.assertIn('Text("上一页"', pager)
        self.assertIn('Text("下一页"', pager)

    def test_all_three_android_sections_use_the_paged_renderer(self):
        for label, home, repo, renderer in (
            ("TV", self.tv_home, self.tv_repo, "PagedLibSection(nav, sec.items"),
            ("mobile", self.mobile_home, self.mobile_repo, "PagedMobileLibSection(nav, sec.items"),
        ):
            with self.subTest(client=label):
                self.assertIn("sections.forEachIndexed", home)
                self.assertIn(renderer, home, "每个返回板块都应交给分页组件")
                order = re.search(r"blockOrder\s*=\s*listOf\(([^)]*)\)", repo)
                self.assertIsNotNone(order, "仓库未声明固定三栏顺序")
                names = tuple(re.findall(r'"([^"]+)"', order.group(1)))
                self.assertEqual(names, EXPECTED_TV_SECTIONS)
                self.assertIn("completeBlocks(sections)", repo)

    def test_channel_sections_fetch_is_confined_to_io_dispatcher(self):
        for label, repo in (("TV", self.tv_repo), ("mobile", self.mobile_repo)):
            with self.subTest(client=label):
                method = _source_block(
                    repo,
                    "suspend fun channelSections(",
                    "/** 源站首页",
                )
                self.assertRegex(
                    method,
                    r"val secs = withContext\(Dispatchers\.IO\)\s*\{\s*"
                    r"runCatching\s*\{\s*VodApiSource\.fetchHome\(",
                    "channelSections 必须在 IO dispatcher 调用同步 fetchHome，避免主线程网络异常被吞",
                )

    def test_web_pages_every_normal_category_two_rows_at_a_time(self):
        categories = re.search(
            r"const VOD_LIB_PAGED_CATEGORIES\s*=\s*new Set\(\[([^]]*)\]\)",
            self.web_app,
        )
        self.assertIsNotNone(categories, "网页版未声明分页分类集合")
        self.assertEqual(
            set(re.findall(r'"([^"]+)"', categories.group(1))),
            EXPECTED_WEB_PAGED_CATEGORIES,
        )
        source_categories = re.search(r"const VOD_CATEGORIES\s*=\s*\[([^;]*)\]", self.web_app)
        self.assertIsNotNone(source_categories)
        category_ids = set(re.findall(r'\["([^"]+)"\s*,', source_categories.group(1)))
        self.assertEqual(category_ids, EXPECTED_WEB_PAGED_CATEGORIES,
                         "所有正常分类都要分页，不能漏回旧单行布局")

        renderer = _source_block(
            self.web_app,
            "function renderVodLib(refresh = false) {",
            "/* 海报竖卡",
        )
        self.assertIn("const columns = state.portrait ? 3 : 7", renderer)
        self.assertIn("pageState.pageSize = columns * 2", renderer)
        self.assertIn('class="lib-page-grid"', renderer)
        self.assertIn("bindVodLibPager(host)", renderer)
        self.assertIn("bindVodLibPage(section, null)", renderer)

        landscape = _css_rule(self.web_css, ".lib-page-grid")
        portrait = _css_rule(self.web_css, "body.portrait .lib-page-grid")
        self.assertRegex(landscape, r"grid-template-columns\s*:\s*repeat\(7,")
        self.assertRegex(landscape, r"grid-template-rows\s*:\s*repeat\(2,")
        self.assertRegex(portrait, r"grid-template-columns\s*:\s*repeat\(3,")
        self.assertRegex(portrait, r"grid-template-rows\s*:\s*repeat\(2,")

    def test_web_renders_all_three_sections_independently(self):
        order = re.search(r"const BLOCK_ORDER\s*=\s*\[([^]]*)\]", self.web_app)
        self.assertIsNotNone(order)
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', order.group(1))), EXPECTED_WEB_SECTIONS)

        complete = _source_block(
            self.web_app,
            "function completeLibBlocks(map) {",
            "function categoryHomeHot(",
        )
        self.assertIn("return BLOCK_ORDER.map(name => ({ name, items: map[name] || [] }))", complete)

        renderer = _source_block(
            self.web_app,
            "function renderVodLib(refresh = false) {",
            "/* 海报竖卡",
        )
        self.assertIn("sections.forEach((s, si) =>", renderer)
        self.assertIn("const key = `${cid}:${si}:${s.name}`", renderer,
                      "各栏分页状态须隔离，不能共享页码")
        self.assertIn('class="lib-page-grid"', renderer)

    def test_web_uses_fresh_library_cache_and_keeps_anime_in_douban_scope(self):
        self.assertIn('return "otvw:lib:v5:" + cid', self.web_app,
                      "new page-state/layout contract must not reuse legacy v4 section cache")
        fetch = _source_block(
            self.web_app,
            "async function fetchLibBlocks(cid, homeSections, refresh = false) {",
            "/* 后端冷缓存先回快照",
        )
        anime_guard = fetch.find('if (cid === "anime") return blocks;')
        self.assertGreaterEqual(anime_guard, 0, "动漫分类不能再混入 HHKan 频道数据")
        hhkan_fetch = fetch.find('"/hhkan/channel-sections/"')
        self.assertGreaterEqual(hhkan_fetch, 0, "缺少好好看三栏回源路径")
        self.assertLess(anime_guard, hhkan_fetch,
                        "动漫必须在调用任意 HHKan 栏目接口前提前返回")

    def test_android_category_switch_uses_short_cache_and_web_keeps_explicit_refresh(self):
        for label, home in (("TV", self.tv_home), ("mobile", self.mobile_home)):
            with self.subTest(client=label):
                self.assertIn("Graph.repo.channelSections(selCatId)", home)
                repo = self.tv_repo if label == "TV" else self.mobile_repo
                self.assertIn("sectionFetchedAt", repo)
                self.assertIn("< 60_000L", repo)

        self.assertIn("fetchLibBlocksSWR(cid, vod.homeSections, paint, refresh)", self.web_app)
        fetch = _source_block(
            self.web_app,
            "async function fetchLibBlocks(cid, homeSections, refresh = false) {",
            "/* 后端冷缓存先回快照",
        )
        self.assertIn('refresh ? "&refresh=1" : ""', fetch)
        self.assertIn("refresh ? 0 : 60000", fetch)
        self.assertIn("renderVodLib(true)", self.web_app)

    def test_android_home_does_not_use_legacy_libblocks_ui_flow(self):
        for label, home in (("TV", self.tv_home), ("mobile", self.mobile_home)):
            with self.subTest(client=label):
                self.assertNotIn("Graph.repo.libBlocksFlow", home)
                self.assertNotRegex(home, r"Graph\.repo\.libBlocks\s*\(")
                self.assertIn("Graph.repo.sectionsFlow", home)

    def test_tv_pager_focus_keys_are_section_scoped_and_retries_are_bounded(self):
        pager = _source_block(
            self.tv_home,
            "private fun PagedLibSection(",
            "/** 片库竖卡",
        )
        self.assertIn("sectionName: String", pager)
        self.assertIn('rows.any { "lib:$sectionName:${it.id}" == returnKey }', pager,
                      "从详情返回时应恢复到正确板块的页")
        self.assertIn('val target = "lib:$sectionName:${pages[next][slot.coerceIn(0, pages[next].lastIndex)].id}"', pager)
        self.assertIn('nav, item, s, cardReg, "lib:$sectionName:${item.id}"', pager,
                      "卡片焦点注册 key 需要带板块名，避免跨栏同 ID 冲突")

        turn = _source_block(pager, "fun turn(next: Int, slot: Int) {", "    Column(")
        self.assertIn("repeat(20)", turn, "换页落焦重试必须有界")
        self.assertIn("delay(40)", turn)
        self.assertIn("runCatching { cardReg.fr(target).requestFocus() }.isSuccess", turn,
                      "Compose requestFocus 返回 Unit；以调用未抛异常作为成功条件")
        self.assertNotIn("getOrDefault(false)", turn)

        lib_card = _source_block(self.tv_home, "private fun LibCard(", "\n/**\n * 最近观看")
        self.assertIn("focusKey: String = item.id", lib_card)
        self.assertIn("ReturnFocus.mark(\"vod\", focusKey)", lib_card)
        self.assertIn("cardReg?.fr(focusKey)", lib_card)

    def test_web_pager_cards_are_not_the_legacy_single_horizontal_library_row(self):
        renderer = _source_block(
            self.web_app,
            "function renderVodLib(refresh = false) {",
            "/* 海报竖卡",
        )
        paged_branch = _source_block(renderer, "if (paged && items.length)", "        return;")
        self.assertIn('class="lib-page-grid"', paged_branch)
        self.assertNotIn('class="hscroll poster-row"', paged_branch)
        # Paged branches must all return before the legacy single-row fallback.


if __name__ == "__main__":
    unittest.main()

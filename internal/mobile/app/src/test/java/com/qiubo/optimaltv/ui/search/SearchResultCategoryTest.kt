package com.qiubo.optimaltv.ui.search

import com.qiubo.optimaltv.data.model.VodItem
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 跑真实的 searchResultCategory / classifySearchResults / RESULT_CATS。
 *
 * 两处回归：一是实时搜索结果 id 形如 `douban:35634451`，不在目录里，旧实现先
 * `itemById` 必然 miss 再拉 `/hhkan/detail/<douban_id>` 打错目录，分类 tab 全空；
 * 二是「综艺」能归类却不在 RESULT_CATS 里，综艺条目只出现在「全部」、点不出来。
 */
class SearchResultCategoryTest {
    private fun vod(id: String, categoryId: String) =
        VodItem(id = id, sourceId = "douban", title = "标题$id", categoryId = categoryId)

    private val mixed = listOf(
        vod("douban:35634451", "douban:variety"),  // 声生不息
        vod("douban:37816820", "douban:variety"),  // 声生不息2026
        vod("douban:1292052", "douban:movie"),
        vod("douban:26647087", "douban:tv"),
        vod("douban:25823945", "douban:anime"),
        vod("douban:3632421", "douban:short"),
    )

    /** 复刻 SearchScreen 的 remember 过滤：选中 tab 后取到哪些条目。 */
    private fun shown(
        items: List<VodItem>,
        catMap: Map<String, String>,
        selectedCat: String,
    ) = if (selectedCat == "全部") items else items.filter { catMap[it.id] == selectedCat }

    // ---------- categoryId 映射 ----------

    @Test
    fun mapsDoubanMediaTypesToResultTabs() {
        assertEquals("电影", searchResultCategory("douban:movie"))
        assertEquals("电视剧", searchResultCategory("douban:tv"))
        assertEquals("动漫", searchResultCategory("douban:anime"))
        assertEquals("综艺", searchResultCategory("douban:variety"))
        assertEquals("短剧", searchResultCategory("douban:short"))
    }

    @Test
    fun keepsLegacyHhkanChannelFallback() {
        assertEquals("电影", searchResultCategory("hhkan:1"))
        assertEquals("电视剧", searchResultCategory("hhkan:2"))
        assertEquals("动漫", searchResultCategory("hhkan:3"))
        assertEquals("综艺", searchResultCategory("hhkan:4"))
        assertEquals("短剧", searchResultCategory("hhkan:6"))
    }

    @Test
    fun unknownCategoryIdIsDeferredToDetail() {
        assertNull(searchResultCategory(""))
        assertNull(searchResultCategory("douban:"))
        assertNull(searchResultCategory("douban:unknown"))
        assertNull(searchResultCategory("hhkan:5"))
    }

    // ---------- 综艺 tab：既在列表里，也点得动 ----------

    @Test
    fun varietyIsAnOfferedResultTabInTheRightOrder() {
        assertEquals(listOf("全部", "电影", "电视剧", "动漫", "综艺", "短剧"), RESULT_CATS)
    }

    @Test
    fun everyOfferedTabCanActuallySelectResults() {
        val map = runBlocking { classifySearchResults(mixed) { error("不该拉详情") } }
        // 每个 tab 都必须筛得出东西，否则就是「有计数、点了空」
        RESULT_CATS.forEach { cat ->
            assertTrue("tab「$cat」筛不出结果", shown(mixed, map, cat).isNotEmpty())
        }
        assertEquals(2, shown(mixed, map, "综艺").size)
    }

    @Test
    fun selectedTabFiltersByClassifiedCategory() {
        val map = runBlocking { classifySearchResults(mixed) { error("不该拉详情") } }
        assertEquals(mixed, shown(mixed, map, "全部"))
        assertEquals(listOf("douban:1292052"), shown(mixed, map, "电影").map { it.id })
        assertEquals(listOf("douban:3632421"), shown(mixed, map, "短剧").map { it.id })
    }

    @Test
    fun varietyRecoverableViaDetailFallbackForLegacyItems() {
        // 旧源条目 categoryId 为空，只能靠详情 meta 里的「综艺」认出来
        val items = listOf(vod("hhkan:9001", ""), vod("hhkan:9002", "hhkan:unknown"))
        val map = runBlocking { classifySearchResults(items) { classifyVod("2026 / 中国 / 综艺", 12) } }
        assertEquals(2, shown(items, map, "综艺").size)
    }

    @Test
    fun noClassifiedValueFallsOutsideTheOfferedTabs() {
        val items = listOf(
            vod("a", "douban:movie"), vod("b", "douban:tv"), vod("c", "douban:anime"),
            vod("d", "douban:variety"), vod("e", "douban:short"),
            vod("f", "hhkan:1"), vod("g", "hhkan:2"), vod("h", "hhkan:3"),
            vod("i", "hhkan:4"), vod("j", "hhkan:6"),
        )
        val map = runBlocking { classifySearchResults(items) { error("不该拉详情") } }
        // 新增分类却忘了加 tab 时，这条会先失败
        val offered = RESULT_CATS.toSet()
        assertFalse(map.values.any { it !in offered })
    }

    // ---------- 覆盖率与兜底预算 ----------

    @Test
    fun liveSearchResultsClassifyWithoutAnyDetailFetch() = runBlocking {
        val items = mixed.filter { it.categoryId == "douban:variety" }
        var detailCalls = 0
        val map = classifySearchResults(items) { detailCalls++; "电影" }

        assertEquals(0, detailCalls)
        assertEquals(2, map.size)
        assertEquals("综艺", map["douban:35634451"])
        assertEquals("综艺", map["douban:37816820"])
    }

    @Test
    fun knownCategoriesApplyToEveryResultNotJustTheFirst24() = runBlocking {
        val items = (1..60).map { vod("douban:$it", "douban:tv") }
        val map = classifySearchResults(items) { error("不该拉详情：categoryId 已可识别") }
        assertEquals(60, map.size)
        assertTrue(map.values.all { it == "电视剧" })
    }

    @Test
    fun detailFallbackStillCoversUnknownCategories() = runBlocking {
        val items = listOf(vod("douban:1", "douban:movie"), vod("bare:2", ""), vod("bare:3", "x"))
        val map = classifySearchResults(items) { item ->
            if (item.id == "bare:2") classifyVod("2022 / 中国 / 连续剧", 0) else "动漫"
        }
        assertEquals("电影", map["douban:1"])
        assertEquals("电视剧", map["bare:2"])
        assertEquals("动漫", map["bare:3"])
    }

    @Test
    fun detailFallbackIsCappedAt24SoSearchStaysResponsive() = runBlocking {
        val items = (1..40).map { vod("bare:$it", "") }
        var detailCalls = 0
        val map = classifySearchResults(items) { detailCalls++; "电影" }
        assertEquals(24, detailCalls)
        assertEquals(24, map.size)
        assertNull(map["bare:25"])
    }

    @Test
    fun detailFallbackFailuresDoNotAbortTheRest() = runBlocking {
        val items = (1..30).map { vod("bare:$it", "") }
        val map = classifySearchResults(items) { if (it.id == "bare:1") null else "电影" }
        assertNull(map["bare:1"])
        assertEquals(23, map.size)
    }

    @Test
    fun portraitTabChunkingFillsTwoEvenRows() {
        // 移动版竖屏每行 3 个；6 个刚好两整行，5 个会留 3+2 的残行
        assertEquals(2, RESULT_CATS.chunked(3).size)
        assertTrue(RESULT_CATS.chunked(3).all { it.size == 3 })
    }
}

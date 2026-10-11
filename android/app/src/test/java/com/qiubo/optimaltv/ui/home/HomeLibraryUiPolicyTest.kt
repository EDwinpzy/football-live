package com.qiubo.optimaltv.ui.home

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class HomeLibraryUiPolicyTest {

    @Test
    fun `落焦索引处理空行奇数尾项和列边界`() {
        assertNull(HomeLibraryUiPolicy.targetIndex(0, 0, 0))
        assertEquals(0, HomeLibraryUiPolicy.targetIndex(1, 1, 7))
        assertEquals(18, HomeLibraryUiPolicy.targetIndex(19, 0, 99))
        assertEquals(17, HomeLibraryUiPolicy.targetIndex(19, 1, 99))
        assertEquals(1, HomeLibraryUiPolicy.targetIndex(20, 1, -1))
        assertEquals(14, HomeLibraryUiPolicy.targetIndex(20, 0, 7))
    }

    @Test
    fun `国漫标签改名并紧跟电视剧`() {
        val ordered = HomeLibraryUiPolicy.orderedCategories(
            listOf(
                "douban:anime" to "3D国漫",
                "douban:movie" to "电影",
                "douban:variety" to "综艺",
                "douban:tv" to "电视剧",
                "douban:short" to "短剧",
            ),
        )

        assertEquals(
            listOf(
                "douban:movie" to "电影",
                "douban:variety" to "综艺",
                "douban:tv" to "电视剧",
                "douban:anime" to "国漫",
                "douban:short" to "短剧",
            ),
            ordered,
        )
    }

    @Test
    fun `影视片库在三栏后保留内嵌全部页`() {
        assertEquals(listOf("最近热门", "最近更新", "豆瓣高分"), HomeLibraryUiPolicy.SECTION_PAGE_TITLES)
        assertEquals(listOf("最近热门", "最近更新", "豆瓣高分", "全部"), HomeLibraryUiPolicy.PAGE_TITLES)
        assertEquals(3, HomeLibraryUiPolicy.ALL_PAGE_INDEX)
    }

    @Test
    fun `TV分类单列上下逐项移动且左右交给页面导航`() {
        assertNull(HomeLibraryUiPolicy.categoryGridTargetIndex(0, com.qiubo.optimaltv.ui.components.NAV_RIGHT, 7))
        assertEquals(1, HomeLibraryUiPolicy.categoryGridTargetIndex(0, com.qiubo.optimaltv.ui.components.NAV_DOWN, 7))
        assertNull(HomeLibraryUiPolicy.categoryGridTargetIndex(0, com.qiubo.optimaltv.ui.components.NAV_LEFT, 7))
        assertNull(HomeLibraryUiPolicy.categoryGridTargetIndex(0, com.qiubo.optimaltv.ui.components.NAV_UP, 7))
        assertEquals(4, HomeLibraryUiPolicy.categoryGridTargetIndex(3, com.qiubo.optimaltv.ui.components.NAV_DOWN, 7))
        assertEquals(2, HomeLibraryUiPolicy.categoryGridTargetIndex(3, com.qiubo.optimaltv.ui.components.NAV_UP, 7))
        assertNull(HomeLibraryUiPolicy.categoryGridTargetIndex(6, com.qiubo.optimaltv.ui.components.NAV_RIGHT, 7))
        assertNull(HomeLibraryUiPolicy.categoryGridTargetIndex(6, com.qiubo.optimaltv.ui.components.NAV_DOWN, 7))
        assertNull(HomeLibraryUiPolicy.categoryGridTargetIndex(0, com.qiubo.optimaltv.ui.components.NAV_RIGHT, 0))
    }

    @Test
    fun `固定页面兼容后端栏目别名并容忍缺栏`() {
        assertEquals("最近热门", HomeLibraryUiPolicy.sourceSectionName(0, listOf("最近热门", "豆瓣高分")))
        assertEquals("近期热门", HomeLibraryUiPolicy.sourceSectionName(0, listOf("近期热门")))
        assertEquals("最近更新", HomeLibraryUiPolicy.sourceSectionName(1, listOf("最近更新")))
        assertEquals("最新上映", HomeLibraryUiPolicy.sourceSectionName(1, listOf("最新上映")))
        assertEquals("最新上线", HomeLibraryUiPolicy.sourceSectionName(1, listOf("最新上线")))
        assertEquals("豆瓣高分", HomeLibraryUiPolicy.sourceSectionName(2, listOf("豆瓣高分")))
        assertNull(HomeLibraryUiPolicy.sourceSectionName(1, listOf("最近热门", "豆瓣高分")))
        assertNull(HomeLibraryUiPolicy.sourceSectionName(3, listOf("最近热门")))
    }
}

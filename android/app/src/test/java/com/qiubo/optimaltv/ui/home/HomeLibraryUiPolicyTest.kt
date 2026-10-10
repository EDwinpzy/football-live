package com.qiubo.optimaltv.ui.home

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
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
    fun `影视片库固定为热门更新豆瓣高分三页`() {
        assertEquals(listOf("最近热门", "最近更新", "豆瓣高分"), HomeLibraryUiPolicy.PAGE_TITLES)
    }

    @Test
    fun `TV分类滚轮首尾停止并突出中心选项`() {
        assertEquals(0, HomeLibraryUiPolicy.moveCategorySelection(0, -1, 5))
        assertEquals(1, HomeLibraryUiPolicy.moveCategorySelection(0, 1, 5))
        assertEquals(3, HomeLibraryUiPolicy.moveCategorySelection(4, -1, 5))
        assertEquals(4, HomeLibraryUiPolicy.moveCategorySelection(4, 1, 5))
        assertNull(HomeLibraryUiPolicy.categoryWheelIndex(-1, 5))
        assertEquals(0, HomeLibraryUiPolicy.categoryWheelIndex(0, 5))
        assertEquals(4, HomeLibraryUiPolicy.categoryWheelIndex(4, 5))
        assertNull(HomeLibraryUiPolicy.categoryWheelIndex(5, 5))
        assertNull(HomeLibraryUiPolicy.categoryWheelIndex(0, 0))
        assertEquals(1.30f, HomeLibraryUiPolicy.categoryWheelScale(0f), 0.001f)
        assertTrue(HomeLibraryUiPolicy.categoryWheelScale(0f) > HomeLibraryUiPolicy.categoryWheelScale(1f))
        assertTrue(HomeLibraryUiPolicy.categoryWheelAlpha(0f) > HomeLibraryUiPolicy.categoryWheelAlpha(1f))
        assertTrue(HomeLibraryUiPolicy.categoryWheelAlpha(1f) > HomeLibraryUiPolicy.categoryWheelAlpha(2f))
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

package com.qiubo.optimaltv.ui.home

import com.qiubo.optimaltv.ui.components.NAV_LEFT
import com.qiubo.optimaltv.ui.components.NAV_RIGHT
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class HomeLibraryUiPolicyTest {

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
    fun `遥控左右键只在可用范围内切换分页`() {
        assertEquals(2, HomeLibraryUiPolicy.targetPageForDpad(1, 3, NAV_RIGHT))
        assertEquals(0, HomeLibraryUiPolicy.targetPageForDpad(1, 3, NAV_LEFT))
        assertNull(HomeLibraryUiPolicy.targetPageForDpad(2, 3, NAV_RIGHT))
        assertNull(HomeLibraryUiPolicy.targetPageForDpad(0, 3, NAV_LEFT))
        assertNull(HomeLibraryUiPolicy.targetPageForDpad(1, 3, -1))
    }

    @Test
    fun `触屏横滑方向与遥控翻页一致并忽略短拖动`() {
        assertEquals(2, HomeLibraryUiPolicy.targetPageForSwipe(1, 3, -60f, 40f))
        assertEquals(0, HomeLibraryUiPolicy.targetPageForSwipe(1, 3, 60f, 40f))
        assertNull(HomeLibraryUiPolicy.targetPageForSwipe(1, 3, -30f, 40f))
        assertNull(HomeLibraryUiPolicy.targetPageForSwipe(2, 3, -60f, 40f))
        assertNull(HomeLibraryUiPolicy.targetPageForSwipe(0, 3, 60f, 40f))
    }
}

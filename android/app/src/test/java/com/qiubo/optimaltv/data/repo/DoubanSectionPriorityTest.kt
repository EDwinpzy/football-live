package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.data.source.HhkanSource.ChannelSection
import org.junit.Assert.assertEquals
import org.junit.Test

class DoubanSectionPriorityTest {
    private fun section(name: String, vararg ids: String) = ChannelSection(
        name, ids.map { VodItem("douban:$it", "douban", it, "douban:movie") },
    )

    @Test
    fun `populated Douban section replaces local items and preserves upstream order`() {
        val local = listOf(section("最近热门", "old", "shared"), section("最新上映", "fallback"))
        val upstream = listOf(section("最近热门", "new", "shared", "newer"))

        val result = preferDoubanSections(local, upstream)

        assertEquals(listOf("douban:new", "douban:shared", "douban:newer"), result[0].items.map { it.id })
        assertEquals(listOf("douban:fallback"), result[1].items.map { it.id })
    }

    @Test
    fun `empty upstream section retains local fallback and new sections are added`() {
        val local = listOf(section("最近热门", "fallback"))
        val upstream = listOf(section("最近热门"), section("豆瓣高分", "rated"))

        val result = preferDoubanSections(local, upstream)

        assertEquals(listOf("douban:fallback"), result[0].items.map { it.id })
        assertEquals(listOf("douban:rated"), result[1].items.map { it.id })
        assertEquals(local, preferDoubanSections(local, emptyList()))
    }
}

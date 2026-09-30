package com.qiubo.optimaltv.ui.search

import com.qiubo.optimaltv.data.model.VodItem
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 搜索结果合并顺序：remote 优先、local 补充。
 *
 * 回归背景：旧表达式 `(localHits + remote).distinctBy { it.id }` 让 local 排在前面。
 * 本地目录按「首次出现的浏览频道」打 categoryId（那是分区不是真实分类），
 * 同 id 时错分类的本地副本会遮住后端已认定 douban:anime 的条目，动漫 tab 搜不到。
 */
class SearchResultMergeTest {
    private fun vod(id: String, title: String, categoryId: String) =
        VodItem(id = id, sourceId = "douban", title = title, categoryId = categoryId)

    @Test
    fun backendIdentifiedResultWinsOverMisfiledLocalCopy() {
        val localHits = listOf(vod("douban:30210221", "鬼灭之刃", "douban:movie"))
        val remote = listOf(vod("douban:30210221", "鬼灭之刃", "douban:anime"))

        val results = mergeSearchResults(localHits, remote)

        assertEquals(1, results.size)
        assertEquals("douban:anime", results[0].categoryId)
    }

    @Test
    fun localOnlyResultsAreKeptAsSupplement() {
        val localHits = listOf(vod("hhkan:1", "本地独家", "douban:variety"))
        val remote = listOf(vod("douban:9", "后端结果", "douban:anime"))

        val results = mergeSearchResults(localHits, remote)

        assertEquals(listOf("douban:9", "hhkan:1"), results.map { it.id })
    }

    @Test
    fun backendResultsComeFirstSoTheyLeadTheGridOrder() {
        val localHits = (1..5).map { vod("hhkan:$it", "本地$it", "douban:movie") }
        val remote = (1..3).map { vod("douban:$it", "后端$it", "douban:anime") }

        val results = mergeSearchResults(localHits, remote)

        // 后端命中的排在前面（优先展示），本地补充在后
        assertEquals(
            listOf("douban:1", "douban:2", "douban:3", "hhkan:1", "hhkan:2", "hhkan:3", "hhkan:4", "hhkan:5"),
            results.map { it.id },
        )
    }

    @Test
    fun emptySidesBehaveLikeBefore() {
        assertEquals(emptyList<String>(), mergeSearchResults(emptyList(), emptyList()).map { it.id })
        val remote = (1..2).map { vod("douban:$it", "t$it", "douban:tv") }
        assertEquals(2, mergeSearchResults(emptyList(), remote).size)
    }
}

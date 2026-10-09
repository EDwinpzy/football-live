package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.data.source.HhkanSource
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Test

/**
 * BUG-03 的读侧：同一内容下有多条可播线路时，L0 记忆必须按 line_index 精确命中。
 *
 * 旧逻辑只比 `sourceId:contentId` 前缀，同一内容的三条线前缀完全相同，
 * `firstOrNull` 永远返回第 0 条——「上次播第 3 条」被静默换成「这次播第 0 条」。
 */
class LastGoodLineMatchTest {

    private fun line(ref: String) = LineInfo(
        vodId = "hhkan:abc",
        label = "line",
        url = "",
        playRef = "ref|$ref|0",
    )

    private val threeLines = listOf(
        line("hhkan:abc:0"),
        line("hhkan:abc:1"),
        line("hhkan:abc:2"),
    )

    @Test
    fun `picks the exact line index that played successfully last time`() {
        val remembered = 2
        val hit = threeLines.firstOrNull { l ->
            val parts = l.playRef.split("|").getOrNull(1)?.split(":")
            parts != null && parts.size == 3 &&
                (parts[2].trim().toIntOrNull() ?: 0) == remembered
        }
        assertEquals("hhkan:abc:2", hit?.playRef?.split("|")?.get(1))
    }

    @Test
    fun `index zero still resolves to the first line`() {
        val hit = threeLines.firstOrNull { l ->
            val parts = l.playRef.split("|").getOrNull(1)?.split(":")
            parts != null && parts.size == 3 &&
                (parts[2].trim().toIntOrNull() ?: 0) == 0
        }
        assertEquals("hhkan:abc:0", hit?.playRef?.split("|")?.get(1))
    }

    @Test
    fun `a remembered index beyond the current list falls back to the first same content line`() {
        val onlyTwo = threeLines.take(2)
        val sameContent = onlyTwo.filter {
            val parts = it.playRef.split("|").getOrNull(1)?.split(":")
            parts != null && parts[0] == "hhkan" && parts[1] == "abc"
        }
        val hit = sameContent.firstOrNull { l ->
            val parts = l.playRef.split("|").getOrNull(1)?.split(":")
            parts != null && parts.size == 3 && (parts[2].trim().toIntOrNull() ?: 0) == 9
        } ?: sameContent.firstOrNull()
        assertEquals("hhkan:abc:0", hit?.playRef?.split("|")?.get(1))
    }

    @Test
    fun `a line with a direct url and no line id is not treated as a match`() {
        val direct = LineInfo(
            vodId = "hhkan:abc",
            label = "direct",
            url = "https://cdn.example/a.m3u8",
            playRef = "ref|direct|0",
        )
        val parts = direct.playRef.split("|").getOrNull(1)?.split(":")
        assertEquals("direct", parts?.first())
        assertEquals(1, parts?.size)
    }

    @Test
    fun `last good line is first while every local alternative remains selectable`() {
        val local = threeLines + line("hhkan:other:0")

        val ordered = LastGoodSourceLinePolicy.preferredFirst(local, "hhkan", "abc", 2)

        assertEquals(listOf("hhkan:abc:2", "hhkan:abc:0", "hhkan:abc:1", "hhkan:other:0"),
            ordered?.map { it.playRef.substringAfter('|').substringBefore('|') })
    }

    @Test
    fun `last good source match compares content id segments rather than prefixes`() {
        val local = listOf(line("hhkan:content-10:0"))

        assertEquals(null, LastGoodSourceLinePolicy.preferredFirst(local, "hhkan", "content-1", 0))
    }

    @Test
    fun `last good identity reads the actual line id from the playRef middle field`() {
        val refreshed = LineInfo(
            vodId = "douban:456",
            label = "线路甲",
            url = "",
            playRef = "456|fresh-provider:fresh-content:4|2",
        )

        assertEquals("fresh-provider:fresh-content:4", PlayableLineIdentity.id(refreshed))
    }

    @Test
    fun `unaccepted refresh does not replace the detail snapshot for visible playRefs`() {
        val store = PlayRefDetailSnapshotStore()
        val visibleLine = LineInfo("douban:456", "线路甲", "", "456|old-source:content|1")
        val original = detail("UI 原始快照")
        val refreshed = detail("后台新快照")

        store.recordCandidate(visibleLine.playRef, original)
        store.accept(listOf(visibleLine))
        assertSame(original, store.get(visibleLine.playRef))

        store.recordCandidate(visibleLine.playRef, refreshed)
        assertSame(original, store.get(visibleLine.playRef))

        store.accept(listOf(visibleLine))
        assertSame(refreshed, store.get(visibleLine.playRef))
    }

    @Test
    fun `synthetic preferred refs keep the requested nonzero episode and local alternatives`() {
        val local = threeLines + line("hhkan:other:0")

        val lastGood = PreferredPlayableLines.reorder(
            lines = local,
            preferredId = "remembered-source:content:3",
            detailRef = "douban:456",
            epIndex = 5,
        )
        val cloudPreferred = PreferredPlayableLines.reorder(
            lines = local,
            preferredId = "cloud-source:content",
            detailRef = "douban:456",
            epIndex = 8,
            preferredLabel = "云端线路甲",
        )

        assertEquals("douban:456|remembered-source:content:3|5", lastGood.first().playRef)
        assertEquals("线路·remembered-source", lastGood.first().label)
        assertEquals(local.map { it.playRef }, lastGood.drop(1).map { it.playRef })
        assertEquals("douban:456|cloud-source:content|8", cloudPreferred.first().playRef)
        assertEquals("云端线路甲", cloudPreferred.first().label)
        assertEquals(local.map { it.playRef }, cloudPreferred.drop(1).map { it.playRef })
    }

    private fun detail(title: String) = HhkanSource.Companion.DetailInfo(
        title = title,
        year = "",
        desc = "",
        poster = "",
        rating = 0.0,
        lines = emptyList(),
    )
}

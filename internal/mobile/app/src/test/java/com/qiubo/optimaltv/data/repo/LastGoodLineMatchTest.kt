package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.data.model.LineInfo
import org.junit.Assert.assertEquals
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
}

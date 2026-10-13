package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.model.LineInfo
import org.junit.Assert.assertEquals
import org.junit.Test

class PlayerSourceGroupingPolicyTest {
    @Test
    fun groupsKeepProviderOrderAndOriginalIndices() {
        val groups = PlayerSourceGroupingPolicy.groups(
            listOf(
                line("好好看·正片"),
                line("非凡资源 · 高清"),
                line("好好看•蓝光"),
                line("独立线路"),
            ),
        )

        assertEquals(listOf("好好看", "非凡资源", "其他信号源"), groups.map { it.provider })
        assertEquals(listOf(listOf(0, 2), listOf(1), listOf(3)), groups.map { it.lineIndices })
    }

    @Test
    fun unprefixedHhkanRoutesUseTheirActualReferenceAsTheFallbackProvider() {
        val hhkanLines = listOf("超清2", "FF线路", "HN线路").mapIndexed { index, label ->
            LineInfo(
                vodId = "hhkan:374249",
                label = label,
                url = "",
                playRef = "hhkan:374249|source$index:content:$index|0",
            )
        }
        val groups = PlayerSourceGroupingPolicy.groups(
            hhkanLines + line("独立信号", vodId = "api:7", playRef = "api:7|channel:4"),
        )

        assertEquals(listOf("好好看", "其他信号源"), groups.map { it.provider })
        assertEquals(listOf(listOf(0, 1, 2), listOf(3)), groups.map { it.lineIndices })
    }

    @Test
    fun groupedRowShowsOnlyThePartAfterItsProvider() {
        assertEquals("高清", PlayerSourceGroupingPolicy.displayLabel("非凡资源 · 高清", "非凡资源"))
        assertEquals("无分组线路", PlayerSourceGroupingPolicy.displayLabel("无分组线路", "其他信号源"))
    }

    private fun line(label: String, vodId: String = "vod:1", playRef: String = ""): LineInfo =
        LineInfo(vodId = vodId, label = label, url = "", playRef = playRef)
}

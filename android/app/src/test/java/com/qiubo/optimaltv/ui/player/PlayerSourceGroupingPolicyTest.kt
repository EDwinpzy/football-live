package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Test

class PlayerSourceGroupingPolicyTest {
    @Test fun hhkanMetadataGroupsUnprefixedLinesWithoutGuessingOtherSources() {
        val groups = PlayerSourceGroupingPolicy.groups(
            listOf("HN线路", "高清", "蓝光", "非凡资源·feifan"),
            listOf("hhkan:374249|2|1", "custom:42", "374249|3|1", "hhkan:374249|4|1"),
        )
        assertEquals(listOf("好好看", "其他信号源", "非凡资源"), groups.map { it.provider })
        assertEquals(listOf(0, 2), groups[0].lineIndices)
    }
    @Test fun interleavedProvidersKeepOriginalPlaybackIndices() {
        val groups = PlayerSourceGroupingPolicy.groups(listOf("好好看·HN", "暴风 · 蓝光", "好好看·LZ", "暴风•高清"))
        assertEquals(listOf("好好看", "暴风"), groups.map { it.provider })
        assertEquals(listOf(0, 2), groups[0].lineIndices)
        assertEquals(listOf(1, 3), groups[1].lineIndices)
    }

    @Test fun unprefixedLinesRemainSelectableInOneFallbackGroup() {
        val groups = PlayerSourceGroupingPolicy.groups(listOf("HN线路", "", "LZ线路"))
        assertEquals(listOf(0, 1, 2), groups.single().lineIndices)
        assertEquals("其他信号源", groups.single().provider)
        assertEquals(emptyList<PlayerSourceGroupingPolicy.Group>(), PlayerSourceGroupingPolicy.groups(emptyList()))
    }
}

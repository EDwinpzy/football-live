package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Test

class PlayerSourceGroupingPolicyTest {
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

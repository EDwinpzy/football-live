package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.data.model.MatchChannel
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class LiveSourcePolicyTest {
    @Test
    fun `backend order is authoritative and remembered dynamic source is retained only when playable`() {
        val ids = listOf("88z_2", "88z_0", "88z_1")

        assertEquals(ids, LiveSourcePolicy.orderedIds(ids))
        assertEquals("88z_1", LiveSourcePolicy.preferred("88z_1", ids))
        assertEquals("88z_2", LiveSourcePolicy.preferred("88z_retired", ids))
    }

    @Test
    fun `match channel metadata does not filter unknown dynamic source ids or names`() {
        val channels = listOf(
            MatchChannel("88z_17", "17", "Bundesliga - Deutsch"),
            MatchChannel("88z_18", "18", "法甲-线路2"),
            MatchChannel("", "19", ""),
        )

        assertEquals(channels.take(2), LiveSourcePolicy.sanitizeChannels(channels))
    }
}

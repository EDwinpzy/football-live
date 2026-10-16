package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.model.LineInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class VodSourcePreferencePolicyTest {
    @Test
    fun `stable source identity follows the selected line after list reordering`() {
        val selected = line("provider-b:content:2", "线路乙")
        val currentLines = listOf(
            line("provider-a:content:0", "线路甲"),
            line("provider-b:content:2", "线路乙"),
        )

        assertEquals("ref:provider-b:content:2", VodSourcePreferencePolicy.identity(selected))
        assertEquals(1, VodSourcePreferencePolicy.preferredIndex(currentLines, "ref:provider-b:content:2"))
        assertEquals(0, VodSourcePreferencePolicy.preferredIndex(currentLines.reversed(), "ref:provider-b:content:2"))
    }

    @Test
    fun `same provider remains selected when episode-specific content ID changes`() {
        val episodeLines = listOf(
            line("provider-a:episode-2:0", "线路甲"),
            line("provider-b:episode-2:0", "线路乙"),
        )

        assertEquals(1, VodSourcePreferencePolicy.preferredIndex(episodeLines, "ref:provider-b:episode-1:3"))
    }

    @Test
    fun `unavailable remembered source does not match a different provider`() {
        val currentLines = listOf(line("provider-a:content:0", "线路甲"))

        assertNull(VodSourcePreferencePolicy.preferredIndex(currentLines, "ref:provider-b:content:0"))
    }

    @Test
    fun `automatic switching is disabled only for ordinary VOD`() {
        assertFalse(AutomaticSourceSwitchPolicy.maySwitch(isLive = false))
        assertTrue(AutomaticSourceSwitchPolicy.maySwitch(isLive = true))
        assertTrue(AutomaticSourceSwitchPolicy.maySwitch(isLive = false, isFootballVod = true))
    }

    @Test
    fun `only ordinary VOD gets the five minute startup timeout`() {
        assertEquals(300_000L, PlaybackStartupTimeoutPolicy.timeoutMs(isLive = false, isFootballVod = false))
        assertEquals(20_000L, PlaybackStartupTimeoutPolicy.timeoutMs(isLive = true, isFootballVod = false))
        assertEquals(20_000L, PlaybackStartupTimeoutPolicy.timeoutMs(isLive = false, isFootballVod = true))
    }

    private fun line(id: String, label: String) = LineInfo(
        vodId = "douban:1",
        label = label,
        url = "",
        playRef = "douban:1|$id|0",
    )
}

package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import com.qiubo.optimaltv.data.repo.SelectedSourceUnavailableException

class ManualLineSelectionPolicyTest {
    @Test
    fun `selecting the active vod line explicitly retries it`() {
        assertTrue(ManualLineSelectionPolicy.shouldStart(lineCount = 1, selected = 0, active = 0, isLive = false))
    }

    @Test
    fun `reselecting the active live line keeps existing live behavior`() {
        assertFalse(ManualLineSelectionPolicy.shouldStart(lineCount = 2, selected = 1, active = 1, isLive = true))
    }

    @Test
    fun `invalid source selection is ignored`() {
        assertFalse(ManualLineSelectionPolicy.shouldStart(lineCount = 1, selected = 1, active = 0, isLive = false))
    }

    @Test
    fun `unmappable fresh selection stays on the selected line while ordinary failures keep failover`() {
        assertEquals(
            ResolutionFailureAction.KEEP_SELECTED_LINE,
            ManualLineSelectionPolicy.resolutionFailureAction(SelectedSourceUnavailableException()),
        )
        assertEquals(
            ResolutionFailureAction.USE_EXISTING_FAILOVER,
            ManualLineSelectionPolicy.resolutionFailureAction(IllegalStateException("ordinary parse failure")),
        )
    }

    @Test
    fun `visible vod lines retain their source labels`() {
        val lines = listOf(
            com.qiubo.optimaltv.data.model.LineInfo("douban:1", "线路甲 · 第1集", "", "ref|line-a|0"),
            com.qiubo.optimaltv.data.model.LineInfo("douban:1", "", "", "ref|line-b|0"),
        )

        assertEquals(listOf("线路甲 · 第1集", "信号源2"), VisibleVodLinePolicy.apply(lines).map { it.label })
    }
}

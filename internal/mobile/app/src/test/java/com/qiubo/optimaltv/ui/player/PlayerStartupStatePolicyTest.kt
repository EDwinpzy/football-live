package com.qiubo.optimaltv.ui.player

import org.junit.Assert.*
import org.junit.Test

class PlayerStartupStatePolicyTest {
    @Test fun footballStartsInLiveModeBeforeAnyNetworkRequest() {
        val state = PlayerStartupStatePolicy.initial("live:129478", 0)
        assertTrue(state.isLive)
        assertTrue(state.sourceResolving)
        assertFalse(state.controlsVisible)
        assertTrue(state.lines.isEmpty())
        assertNull(state.fatalMsg)
    }

    @Test fun vodKeepsItsEpisodeAndPointPlaybackControls() {
        val state = PlayerStartupStatePolicy.initial("douban:37457082", 2)
        assertFalse(state.isLive)
        assertFalse(state.sourceResolving)
        assertEquals(2, state.epIndex)
    }

    @Test fun aTitleContainingLiveDoesNotSelectFootballMode() {
        assertFalse(PlayerStartupStatePolicy.initial("hhkan:live", 0).isLive)
    }
}

package com.qiubo.optimaltv.ui.player

import org.junit.Assert.*
import org.junit.Test

class PlayerStartupStatePolicyTest {
    @Test fun footballStartsInLiveModeBeforeAnyNetworkRequest() {
        val state = PlayerStartupStatePolicy.initial("129478")
        assertTrue(state.isLive)
        assertTrue(state.sourceResolving)
        assertFalse(state.controlsVisible)
        assertTrue(state.lines.isEmpty())
        assertNull(state.fatalMsg)
    }

}

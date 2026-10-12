package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PlaybackSessionPolicyTest {
    @Test
    fun `asynchronous result is accepted only by the same generation line and engine`() {
        assertTrue(PlaybackSessionPolicy.isCurrent(4, 4, 2, 2, 7, 7))
        assertFalse(PlaybackSessionPolicy.isCurrent(3, 4, 2, 2, 7, 7))
        assertFalse(PlaybackSessionPolicy.isCurrent(4, 4, 1, 2, 7, 7))
        assertFalse(PlaybackSessionPolicy.isCurrent(4, 4, 2, 2, 6, 7))
    }

    @Test
    fun `a live ended window gets one fresh same source restart then stops`() {
        assertEquals(
            PlaybackSessionPolicy.LiveEndAction.RESTART_CURRENT,
            PlaybackSessionPolicy.actionAfterLiveEnd(recoveryCount = 0),
        )
        assertEquals(
            PlaybackSessionPolicy.LiveEndAction.STOP,
            PlaybackSessionPolicy.actionAfterLiveEnd(recoveryCount = 1),
        )
        assertEquals(
            PlaybackSessionPolicy.LiveEndAction.STOP,
            PlaybackSessionPolicy.actionAfterLiveEnd(recoveryCount = 10),
        )
    }
}

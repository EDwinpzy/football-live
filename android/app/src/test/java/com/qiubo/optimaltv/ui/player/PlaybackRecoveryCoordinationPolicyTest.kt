package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PlaybackRecoveryCoordinationPolicyTest {
    @Test
    fun `recovery waits for active prepare and any in flight recovery`() {
        assertFalse(
            PlaybackRecoveryCoordinationPolicy.canStartRecovery(
                startedThisSession = false,
                awaitingPrepare = false,
                sourceResolving = false,
                recoveryInFlight = false,
            ),
        )
        assertFalse(
            PlaybackRecoveryCoordinationPolicy.canStartRecovery(
                startedThisSession = true,
                awaitingPrepare = true,
                sourceResolving = false,
                recoveryInFlight = false,
            ),
        )
        assertFalse(
            PlaybackRecoveryCoordinationPolicy.canStartRecovery(
                startedThisSession = true,
                awaitingPrepare = false,
                sourceResolving = true,
                recoveryInFlight = false,
            ),
        )
        assertFalse(
            PlaybackRecoveryCoordinationPolicy.canStartRecovery(
                startedThisSession = true,
                awaitingPrepare = false,
                sourceResolving = false,
                recoveryInFlight = true,
            ),
        )
        assertTrue(
            PlaybackRecoveryCoordinationPolicy.canStartRecovery(
                startedThisSession = true,
                awaitingPrepare = false,
                sourceResolving = false,
                recoveryInFlight = false,
            ),
        )
    }

    @Test
    fun `advancing video frames prevent clock only false stall`() {
        var ticks = 0
        repeat(20) { frame ->
            ticks = PlaybackRecoveryCoordinationPolicy.nextReadyStallTicks(
                ticks = ticks,
                previousPositionMs = 0L,
                positionMs = 0L,
                previousRenderedFrames = frame,
                renderedFrames = frame + 1,
            )
        }
        assertEquals(0, ticks)
    }

    @Test
    fun `missing VLC frame stats keep clock based stall fallback`() {
        var ticks = 0
        repeat(8) {
            ticks = PlaybackRecoveryCoordinationPolicy.nextReadyStallTicks(
                ticks = ticks,
                previousPositionMs = 0L,
                positionMs = 0L,
                previousRenderedFrames = -1,
                renderedFrames = -1,
            )
        }
        assertEquals(8, ticks)
    }

    @Test
    fun `either advancing clock or rendered frames is stable playback progress`() {
        assertTrue(PlaybackRecoveryCoordinationPolicy.hasPlaybackProgress(true, false))
        assertTrue(PlaybackRecoveryCoordinationPolicy.hasPlaybackProgress(false, true))
        assertFalse(PlaybackRecoveryCoordinationPolicy.hasPlaybackProgress(false, false))
        assertTrue(PlaybackRecoveryCoordinationPolicy.renderedFramesAdvanced(4, 5))
        assertFalse(PlaybackRecoveryCoordinationPolicy.renderedFramesAdvanced(-1, 5))
        assertFalse(PlaybackRecoveryCoordinationPolicy.renderedFramesAdvanced(5, 5))
    }

    @Test
    fun `negative live timeline still detects a stalled decoder`() {
        assertEquals(4, PlaybackRecoveryCoordinationPolicy.nextReadyStallTicks(3, -3000L, -3000L, 10, 10))
        assertEquals(0, PlaybackRecoveryCoordinationPolicy.nextReadyStallTicks(3, -3000L, -3000L, 10, 11))
    }
}

package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class FirstFrameRecoveryPolicyTest {

    @Test fun `VOD has no startup deadline while live keeps its watchdog`() {
        assertEquals(20_000L, PlayerViewModel.START_WATCHDOG_MS)
    }

    @Test fun `first frame recovery keeps the selected source`() {
        assertEquals(FirstFrameRecoveryPolicy.Action.WAIT, FirstFrameRecoveryPolicy.action(2_999))
        assertEquals(FirstFrameRecoveryPolicy.Action.PREP_BACKUP, FirstFrameRecoveryPolicy.action(3_000))
        assertEquals(FirstFrameRecoveryPolicy.Action.PREP_BACKUP, FirstFrameRecoveryPolicy.action(6_999))
        assertEquals(
            FirstFrameRecoveryPolicy.Action.ENGINE_FALLBACK,
            FirstFrameRecoveryPolicy.action(7_000, hasBackup = true),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Action.ENGINE_FALLBACK,
            FirstFrameRecoveryPolicy.action(7_000, hasBackup = false),
        )
    }

    @Test fun `VLC keeps waiting on the same source indefinitely`() {
        assertEquals(
            FirstFrameRecoveryPolicy.Action.WAIT,
            FirstFrameRecoveryPolicy.action(
                Long.MAX_VALUE,
                hasBackup = true,
                onMedia3 = false,
                vlcAlreadyTried = true,
            ),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Action.WAIT,
            FirstFrameRecoveryPolicy.action(
                Long.MAX_VALUE,
                hasBackup = true,
                onMedia3 = true,
                vlcAlreadyTried = true,
            ),
        )
    }

    @Test fun `VOD replay does not switch sources or time out`() {
        assertEquals(
            FirstFrameRecoveryPolicy.Action.ENGINE_FALLBACK,
            FirstFrameRecoveryPolicy.footballAction(
                7_000, hasBackup = true, onMedia3 = true, vlcAlreadyTried = false,
            ),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Action.WAIT,
            FirstFrameRecoveryPolicy.footballAction(
                Long.MAX_VALUE, hasBackup = true, onMedia3 = false, vlcAlreadyTried = true,
            ),
        )
    }

    @Test fun `a preferred or last good source still starts without waiting for all lines`() {
        assertTrue(FirstFrameRecoveryPolicy.shouldStartImmediately(true, 0))
        assertTrue(FirstFrameRecoveryPolicy.shouldStartImmediately(false, 1))
        assertFalse(FirstFrameRecoveryPolicy.shouldStartImmediately(false, 0))
    }

    @Test fun `a stale ladder from the previous session cannot act on the current one`() {
        val ladder = FirstFrameRecoveryLadderTracker()
        val oldGeneration = ladder.beginGeneration()
        val currentGeneration = ladder.beginGeneration()

        assertFalse(ladder.isCurrent(oldGeneration))
        assertTrue(ladder.isCurrent(currentGeneration))
        assertTrue(ladder.acceptEscalation(currentGeneration, FirstFrameRecoveryPolicy.Action.ENGINE_FALLBACK))
        assertFalse(ladder.acceptEscalation(currentGeneration, FirstFrameRecoveryPolicy.Action.PREP_BACKUP))
    }
}

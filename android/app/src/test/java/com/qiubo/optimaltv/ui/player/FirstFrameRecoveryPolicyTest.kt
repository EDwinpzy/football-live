package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class FirstFrameRecoveryPolicyTest {

    private object Spec {
        const val NORMAL_MS = 3_000L
        const val PREP_BACKUP_MS = 5_000L
        const val SWITCH_MS = 7_000L
        const val HARD_TIMEOUT_MS = 300_000L
    }

    @Test fun `the startup timeout is five minutes`() {
        assertEquals(Spec.HARD_TIMEOUT_MS, FirstFrameRecoveryPolicy.HARD_TIMEOUT_MS)
        assertEquals(Spec.HARD_TIMEOUT_MS, PlayerViewModel.VOD_START_WATCHDOG_MS)
        assertEquals(20_000L, PlayerViewModel.START_WATCHDOG_MS)
        assertEquals(15_000L, FirstFrameRecoveryPolicy.FOOTBALL_HARD_TIMEOUT_MS)
    }

    @Test fun `wait stages never select another source`() {
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

    @Test fun `VLC keeps waiting on the same source until the five minute timeout`() {
        assertEquals(
            FirstFrameRecoveryPolicy.Action.WAIT,
            FirstFrameRecoveryPolicy.action(
                Spec.HARD_TIMEOUT_MS - 1,
                hasBackup = true,
                onMedia3 = false,
                vlcAlreadyTried = true,
            ),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Action.HARD_TIMEOUT,
            FirstFrameRecoveryPolicy.action(
                Spec.HARD_TIMEOUT_MS,
                hasBackup = true,
                onMedia3 = false,
                vlcAlreadyTried = true,
            ),
        )
    }

    @Test fun `hard timeout only allows an untried same source engine fallback`() {
        assertEquals(
            FirstFrameRecoveryPolicy.HardTimeoutAction.ENGINE_FALLBACK,
            FirstFrameRecoveryPolicy.hardTimeoutAction(onMedia3 = true, vlcAlreadyTried = false),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.HardTimeoutAction.GIVE_UP,
            FirstFrameRecoveryPolicy.hardTimeoutAction(onMedia3 = false, vlcAlreadyTried = true),
        )
    }

    @Test fun `football replay keeps its previous automatic source recovery thresholds`() {
        assertEquals(
            FirstFrameRecoveryPolicy.Action.SWITCH,
            FirstFrameRecoveryPolicy.footballAction(7_000, hasBackup = true, onMedia3 = true, vlcAlreadyTried = false),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Action.HARD_TIMEOUT,
            FirstFrameRecoveryPolicy.footballAction(
                15_000, hasBackup = true, onMedia3 = true, vlcAlreadyTried = false,
            ),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.FootballHardTimeoutAction.SWITCH_LINE,
            FirstFrameRecoveryPolicy.footballHardTimeoutAction(
                hasBackupLine = true, onMedia3 = true, vlcAlreadyTried = false,
            ),
        )
    }

    @Test fun `hard timeout predicate has exact five minute boundary`() {
        assertFalse(FirstFrameRecoveryPolicy.isHardTimeout(Spec.HARD_TIMEOUT_MS - 1))
        assertTrue(FirstFrameRecoveryPolicy.isHardTimeout(Spec.HARD_TIMEOUT_MS))
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

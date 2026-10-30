package com.qiubo.optimaltv

import com.qiubo.optimaltv.playback.PlaybackRecoveryPolicy
import com.qiubo.optimaltv.playback.RecoveryAction
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PlaybackRecoveryPolicyTest {
    @Test fun `third available line is tried before rebuilding the failing second line`() {
        assertEquals(RecoveryAction.REFRESH_CURRENT, PlaybackRecoveryPolicy.action(0, hasAlternate = true))
        assertEquals(RecoveryAction.SWITCH_SOURCE, PlaybackRecoveryPolicy.action(1, hasAlternate = true))
        assertEquals(RecoveryAction.SWITCH_SOURCE, PlaybackRecoveryPolicy.action(2, hasAlternate = true))
        assertEquals(RecoveryAction.RECREATE_ENGINE, PlaybackRecoveryPolicy.action(2, hasAlternate = false))
    }

    @Test fun `live recovery refreshes switches recreates and resets with a hard budget`() {
        val expected = listOf(
            RecoveryAction.REFRESH_CURRENT,
            RecoveryAction.SWITCH_SOURCE,
            RecoveryAction.RECREATE_ENGINE,
            RecoveryAction.RESET_RUNTIME,
            RecoveryAction.GIVE_UP,
        )
        assertEquals(expected, (0..4).map(PlaybackRecoveryPolicy::action))
    }

    @Test fun `live line failure retries once then switches to an available source`() {
        assertEquals(
            RecoveryAction.REFRESH_CURRENT,
            PlaybackRecoveryPolicy.liveLineFailureAction(retryCount = 0, hasAlternate = true),
        )
        assertEquals(
            RecoveryAction.SWITCH_SOURCE,
            PlaybackRecoveryPolicy.liveLineFailureAction(retryCount = 1, hasAlternate = true),
        )
        assertEquals(
            RecoveryAction.GIVE_UP,
            PlaybackRecoveryPolicy.liveLineFailureAction(retryCount = 1, hasAlternate = false),
        )
    }

    @Test fun `buffering threshold and session generation reject premature or stale recovery`() {
        assertFalse(PlaybackRecoveryPolicy.isStalled(7_999L))
        assertTrue(PlaybackRecoveryPolicy.isStalled(8_000L))
        val generation = PlaybackRecoveryPolicy.Generation()
        val first = generation.next()
        val second = generation.next()
        assertFalse(generation.accepts(first))
        assertTrue(generation.accepts(second))
    }
}


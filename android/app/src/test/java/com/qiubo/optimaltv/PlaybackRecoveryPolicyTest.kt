package com.qiubo.optimaltv

import com.qiubo.optimaltv.playback.PlaybackKind
import com.qiubo.optimaltv.playback.PlaybackRecoveryPolicy
import com.qiubo.optimaltv.playback.RecoveryAction
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PlaybackRecoveryPolicyTest {
    @Test fun `live recovery refreshes switches recreates and resets with a hard budget`() {
        val expected = listOf(
            RecoveryAction.REFRESH_CURRENT,
            RecoveryAction.SWITCH_SOURCE,
            RecoveryAction.RECREATE_ENGINE,
            RecoveryAction.RESET_RUNTIME,
            RecoveryAction.GIVE_UP,
        )
        assertEquals(expected, (0..4).map { PlaybackRecoveryPolicy.action(PlaybackKind.LIVE, it) })
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

    @Test fun `vod recovery reloads then refreshes and keeps resume context`() {
        assertEquals(RecoveryAction.RELOAD_CURRENT, PlaybackRecoveryPolicy.action(PlaybackKind.VOD, 0))
        assertEquals(RecoveryAction.REFRESH_CURRENT, PlaybackRecoveryPolicy.action(PlaybackKind.VOD, 1))
        assertEquals(RecoveryAction.SWITCH_SOURCE, PlaybackRecoveryPolicy.action(PlaybackKind.VOD, 2))
        assertEquals(118_000L, PlaybackRecoveryPolicy.resumePosition(120_000L))
        assertEquals(0L, PlaybackRecoveryPolicy.resumePosition(1_000L))
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


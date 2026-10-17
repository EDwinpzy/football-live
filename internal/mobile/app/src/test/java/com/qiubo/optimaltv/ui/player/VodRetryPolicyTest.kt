package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VodRetryPolicyTest {
    @Test
    fun `every VOD retries until the user leaves and has no startup watchdog`() {
        assertTrue(VodRetryPolicy.retriesUntilUserLeaves(isLive = false))
        assertFalse(VodRetryPolicy.shouldUseStartupWatchdog(isLive = false))
    }

    @Test
    fun `live retains its separate recovery flow while every VOD keeps retrying`() {
        assertFalse(VodRetryPolicy.retriesUntilUserLeaves(isLive = true))
        assertTrue(VodRetryPolicy.shouldUseStartupWatchdog(isLive = true))
    }

    @Test
    fun `boot exceptions keep the VOD retry loop alive without showing a fatal state`() {
        assertTrue(VodRetryPolicy.shouldRetryBootAfterAttempt(
            isLive = false,
            hasFatalMessage = false,
            attemptFailed = true,
        ))
        assertTrue(VodRetryPolicy.shouldRetryBootAfterAttempt(
            isLive = false,
            hasFatalMessage = true,
            attemptFailed = false,
        ))
        assertFalse(VodRetryPolicy.shouldRetryBootAfterAttempt(
            isLive = false,
            hasFatalMessage = false,
            attemptFailed = false,
        ))
        assertFalse(VodRetryPolicy.shouldRetryBootAfterAttempt(
            isLive = true,
            hasFatalMessage = true,
            attemptFailed = true,
        ))
    }

    @Test
    fun `retry prefers the user's pending seek over a stale engine position`() {
        assertEquals(1_200_000L, VodRetryPolicy.retryPositionMs(800_000L, 1_200_000L))
        assertEquals(800_000L, VodRetryPolicy.retryPositionMs(800_000L, null))
        assertEquals(0L, VodRetryPolicy.retryPositionMs(-1L, null))
    }

    @Test
    fun `seek target remains pending until the engine reaches it`() {
        assertFalse(VodRetryPolicy.reachedSeekTarget(600_000L, 1_200_000L))
        assertTrue(VodRetryPolicy.reachedSeekTarget(1_198_000L, 1_200_000L))
    }

    @Test
    fun `same episode resolution resumes at pending seek or current progress`() {
        assertEquals(
            1_200_000L,
            VodRetryPolicy.resolvedEpisodeStartPositionMs(
                targetEpisodeIndex = 4,
                currentEpisodeIndex = 4,
                currentPositionMs = 800_000L,
                pendingSeekTargetMs = 1_200_000L,
            ),
        )
        assertEquals(
            800_000L,
            VodRetryPolicy.resolvedEpisodeStartPositionMs(
                targetEpisodeIndex = 4,
                currentEpisodeIndex = 4,
                currentPositionMs = 800_000L,
                pendingSeekTargetMs = null,
            ),
        )
    }

    @Test
    fun `changing episodes starts from the beginning`() {
        assertEquals(
            0L,
            VodRetryPolicy.resolvedEpisodeStartPositionMs(
                targetEpisodeIndex = 5,
                currentEpisodeIndex = 4,
                currentPositionMs = 800_000L,
                pendingSeekTargetMs = 1_200_000L,
            ),
        )
    }

    @Test
    fun `episode change maps to the unique selected source`() {
        assertEquals(
            1,
            VodEpisodeSourcePolicy.matchingLineIndex(
                selectedSourceIdentity = "source-a",
                candidateIdentities = listOf("source-b", "source-a"),
            ),
        )
        assertEquals(
            null,
            VodEpisodeSourcePolicy.matchingLineIndex(
                selectedSourceIdentity = null,
                candidateIdentities = listOf("source-b", "source-a"),
            ),
        )
    }

    @Test
    fun `missing selected episode stays unresolved instead of switching to another source`() {
        assertEquals(
            null,
            VodEpisodeSourcePolicy.matchingLineIndex(
                selectedSourceIdentity = "source-a",
                candidateIdentities = listOf("source-b", null),
            ),
        )
    }

    @Test
    fun `duplicate selected source identities are ambiguous and stay unresolved`() {
        assertEquals(
            null,
            VodEpisodeSourcePolicy.matchingLineIndex(
                selectedSourceIdentity = "source-a",
                candidateIdentities = listOf("source-a", "source-b", "source-a"),
            ),
        )
    }
}

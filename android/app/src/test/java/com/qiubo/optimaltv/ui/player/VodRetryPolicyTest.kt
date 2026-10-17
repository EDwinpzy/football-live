package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VodRetryPolicyTest {
    @Test
    fun `every VOD uses continuous retry without a startup timeout`() {
        assertTrue(VodRetryPolicy.shouldRetry(isLive = false))
        assertFalse(VodRetryPolicy.shouldUseStartupWatchdog(isLive = false))
        assertFalse(VodRetryPolicy.shouldRetry(isLive = true))
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
    fun `pending seek target is kept while engine position is stale`() {
        assertEquals(184_000L, VodRetryPolicy.resumePositionMs(184_000L, 12_000L))
        assertEquals(12_000L, VodRetryPolicy.resumePositionMs(null, 12_000L))
        assertEquals(0L, VodRetryPolicy.resumePositionMs(null, -4_000L))
    }

    @Test
    fun `seek target is acknowledged only when engine reaches it`() {
        assertFalse(VodRetryPolicy.reachedSeekTarget(600_000L, 1_200_000L))
        assertTrue(VodRetryPolicy.reachedSeekTarget(1_198_000L, 1_200_000L))
    }

    @Test
    fun `new retry generations invalidate delayed work from older selections`() {
        val generations = VodRetryGeneration()
        val old = generations.begin()
        val current = generations.begin()

        assertFalse(generations.isCurrent(old))
        assertTrue(generations.isCurrent(current))

        generations.invalidate()
        assertFalse(generations.isCurrent(current))
    }

    @Test
    fun `episode source matching follows identity and reports a missing source`() {
        assertEquals(1, VodRetryPolicy.matchingSourceIndex("source-b", listOf("source-a", "source-b")))
        assertEquals(null, VodRetryPolicy.matchingSourceIndex("source-a", listOf("source-b")))
        assertEquals(null, VodRetryPolicy.matchingSourceIndex(null, listOf("source-a")))
    }
}

package com.qiubo.optimaltv.ui.player

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import org.junit.Assert.*
import org.junit.Test

class PlaybackSideEffectTest {
    @Test fun optionalFailureDoesNotAbortFollowingWork() = runBlocking {
        val failures = mutableListOf<Exception>()
        var nextCompleted = false
        isolatePlaybackSideEffect(failures::add) { throw IllegalStateException("unavailable") }
        isolatePlaybackSideEffect(failures::add) { nextCompleted = true }
        assertTrue(nextCompleted)
        assertEquals(1, failures.size)
    }

    @Test fun cancellationIsNeverReportedAsOptionalFailure() = runBlocking {
        var reported = false
        val expected = CancellationException("session replaced")
        try {
            isolatePlaybackSideEffect({ reported = true }) { throw expected }
            fail("cancellation must propagate")
        } catch (actual: CancellationException) {
            assertSame(expected, actual)
        }
        assertFalse(reported)
    }

    @Test fun blockingFailureAfterCancellationStillCancelsSession() = runBlocking {
        var reported = false
        try {
            withContext(Job()) {
                isolatePlaybackSideEffect({ reported = true }) {
                    currentCoroutineContext()[Job]!!.cancel()
                    throw IllegalStateException("interrupted IO")
                }
            }
            fail("cancelled session must not continue")
        } catch (_: CancellationException) { }
        assertFalse(reported)
    }
}

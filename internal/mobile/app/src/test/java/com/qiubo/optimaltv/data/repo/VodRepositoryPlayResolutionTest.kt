package com.qiubo.optimaltv.data.repo

import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VodRepositoryPlayResolutionTest {
    @Test
    fun `cancelling a blocking play lookup prevents cache and refreshed line commits`() = runBlocking {
        val enteredFetch = CountDownLatch(1)
        val releaseFetch = CountDownLatch(1)
        val playCacheWritten = AtomicBoolean(false)
        val refreshedLineWritten = AtomicBoolean(false)

        val request = async(Dispatchers.IO) {
            runInterruptibleAndCommit(
                fetch = {
                    enteredFetch.countDown()
                    try {
                        releaseFetch.await(5, TimeUnit.SECONDS)
                    } catch (_: InterruptedException) {
                        // Simulate a blocking client that finishes despite interruption.
                        releaseFetch.await(5, TimeUnit.SECONDS)
                    }
                    "fresh result"
                },
                commit = {
                    playCacheWritten.set(true)
                    refreshedLineWritten.set(true)
                },
            )
        }

        assertTrue(withContext(Dispatchers.IO) { enteredFetch.await(5, TimeUnit.SECONDS) })
        request.cancel()
        releaseFetch.countDown()
        request.cancelAndJoin()

        val result = runCatching { request.await() }.exceptionOrNull()
        assertTrue("cancellation must reach the caller", result is CancellationException)
        assertFalse("cancelled results must not enter playCache", playCacheWritten.get())
        assertFalse("cancelled results must not update the refreshed line ID", refreshedLineWritten.get())
    }
}

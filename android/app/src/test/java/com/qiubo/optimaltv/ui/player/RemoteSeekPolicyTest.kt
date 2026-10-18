package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class RemoteSeekPolicyTest {
    @Test fun `forward repeats accumulate from intent despite stale zero engine samples`() {
        var pending: Long? = null
        repeat(30) {
            pending = RemoteSeekPolicy.target(if (it == 0) 60_000L else 0L, pending, 10_000L, 3_600_000L)
        }
        assertEquals(360_000L, pending)
    }

    @Test fun `hold accelerates in both directions and tap resets to normal`() {
        val times = listOf(0L, 2_000L, 4_000L, 6_000L, 8_000L)
        val expected = listOf(10_000L, 20_000L, 40_000L, 80_000L, 120_000L)
        assertEquals(expected, times.map { RemoteSeekPolicy.acceleratedDelta(10_000L, it) })
        assertEquals(expected.map { -it }, times.map { RemoteSeekPolicy.acceleratedDelta(-10_000L, it) })
        assertEquals(10_000L, RemoteSeekPolicy.acceleratedDelta(10_000L, 0L))
    }

    @Test fun `missing and tiny timelines cannot rewind to zero`() {
        for (duration in listOf(-1L, 0L, 1L, 999L, 1_000L)) {
            assertNull(RemoteSeekPolicy.target(60_000L, 90_000L, 10_000L, duration))
        }
        assertNull(RemoteSeekPolicy.target(60_000L, null, 10_000L, 2_000L))
        assertNull(RemoteSeekPolicy.target(60_000L, 90_000L, -10_000L, 2_000L))
    }

    @Test fun `bounds and extreme deltas remain safe`() {
        assertEquals(99_000L, RemoteSeekPolicy.target(90_000L, null, Long.MAX_VALUE, 100_000L))
        assertEquals(0L, RemoteSeekPolicy.target(90_000L, null, Long.MIN_VALUE, 100_000L))
        assertEquals(40_000L, RemoteSeekPolicy.target(0L, 60_000L, -20_000L, 100_000L))
    }
}

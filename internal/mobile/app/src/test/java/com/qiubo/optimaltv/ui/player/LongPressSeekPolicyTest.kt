package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LongPressSeekPolicyTest {
    @Test
    fun `hold seek accelerates exponentially and caps the step`() {
        assertEquals(1_000L, LongPressSeekPolicy.stepMs(0L))
        assertEquals(1_000L, LongPressSeekPolicy.stepMs(999L))
        assertEquals(2_000L, LongPressSeekPolicy.stepMs(1_000L))
        assertEquals(4_000L, LongPressSeekPolicy.stepMs(2_000L))
        assertEquals(32_000L, LongPressSeekPolicy.stepMs(5_000L))
        assertEquals(32_000L, LongPressSeekPolicy.stepMs(60_000L))
    }

    @Test
    fun `targets accumulate from the pending position and stay inside the seek range`() {
        assertEquals(15_000L, LongPressSeekPolicy.target(10_000L, 20_000L, 1, 5_000L))
        assertEquals(5_000L, LongPressSeekPolicy.target(10_000L, 20_000L, -1, 5_000L))
        assertEquals(19_000L, LongPressSeekPolicy.target(18_000L, 20_000L, 1, 5_000L))
        assertEquals(0L, LongPressSeekPolicy.target(2_000L, 20_000L, -1, 5_000L))
        assertEquals(1_000L, LongPressSeekPolicy.target(2_000L, 20_000L, -1, 5_000L, minimumTargetMs = 1_000L))
        assertEquals(500L, LongPressSeekPolicy.target(500L, 20_000L, -1, 5_000L, minimumTargetMs = 500L))
        assertNull(LongPressSeekPolicy.target(0L, 999L, 1, 1_000L))
        assertNull(LongPressSeekPolicy.target(0L, 20_000L, 0, 1_000L))
        assertNull(LongPressSeekPolicy.target(0L, 20_000L, 1, 0L))
    }

    @Test
    fun `pending target becomes the base for the next repeated seek`() {
        val pending = PendingSeekTarget()
        assertEquals(10_000L, pending.base(10_000L))
        pending.update(20_000L)
        assertEquals(20_000L, pending.base(10_500L))
        pending.clear()
        assertNull(pending.targetMs)
        assertEquals(10_500L, pending.base(10_500L))
    }

    @Test
    fun `seek dispatch coalesces requests and stall recovery waits through hold and grace`() {
        assertEquals(0L, LongPressSeekPolicy.dispatchDelayMs(0L, 100L))
        assertEquals(350L, LongPressSeekPolicy.dispatchDelayMs(1_000L, 1_400L))
        assertEquals(0L, LongPressSeekPolicy.dispatchDelayMs(1_000L, 1_800L))
        assertTrue(LongPressSeekPolicy.shouldDeferStallRecovery(true, 0L, 10_000L))
        assertTrue(LongPressSeekPolicy.shouldDeferStallRecovery(false, 10_000L, 24_999L))
        assertFalse(LongPressSeekPolicy.shouldDeferStallRecovery(false, 10_000L, 25_000L))
        assertFalse(LongPressSeekPolicy.shouldDeferStallRecovery(false, 10_000L, 9_000L))
    }

    @Test
    fun `position confirmation requires reaching the target in the requested direction`() {
        assertFalse(LongPressSeekPolicy.positionConfirmsTarget(10_000L, 11_000L, fromMs = 10_000L))
        assertTrue(LongPressSeekPolicy.positionConfirmsTarget(10_500L, 11_000L, fromMs = 10_000L))
        assertFalse(LongPressSeekPolicy.positionConfirmsTarget(10_000L, 9_000L, fromMs = 10_000L))
        assertTrue(LongPressSeekPolicy.positionConfirmsTarget(9_500L, 9_000L, fromMs = 10_000L))
    }

    @Test
    fun `seek recovery prefers pending target and only pins ordinary vod line`() {
        assertEquals(42_000L, SeekRecoveryPolicy.restorePosition(42_000L, 10_000L))
        assertEquals(8_000L, SeekRecoveryPolicy.restorePosition(null, 10_000L))
        assertTrue(SeekRecoveryPolicy.keepSelectedLine(isLive = false, isFootballVod = false, pendingTargetMs = 42_000L))
        assertFalse(SeekRecoveryPolicy.keepSelectedLine(isLive = false, isFootballVod = false, pendingTargetMs = null))
        assertFalse(SeekRecoveryPolicy.keepSelectedLine(isLive = true, isFootballVod = false, pendingTargetMs = 42_000L))
        assertFalse(SeekRecoveryPolicy.keepSelectedLine(isLive = false, isFootballVod = true, pendingTargetMs = 42_000L))
    }
}

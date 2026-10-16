package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SeekAccelerationPolicyTest {
    @Test
    fun `held key accelerates and caps at configured multiplier`() {
        val first = SeekAccelerationPolicy.nextStep(
            direction = 1,
            repeatCount = 0,
            eventTimeMs = 1_000L,
            state = SeekAccelerationPolicy.State(),
        )!!
        assertEquals(10_000L, first.deltaMs)

        val afterHalfSecond = SeekAccelerationPolicy.nextStep(
            direction = 1,
            repeatCount = 1,
            eventTimeMs = 1_500L,
            state = first.state,
        )!!
        assertEquals(20_000L, afterHalfSecond.deltaMs)

        var heldState = afterHalfSecond.state
        var afterLongHold = afterHalfSecond
        for (timeMs in 2_000L..4_500L step 500L) {
            afterLongHold = SeekAccelerationPolicy.nextStep(
                direction = 1,
                repeatCount = 1,
                eventTimeMs = timeMs,
                state = heldState,
            )!!
            heldState = afterLongHold.state
        }
        assertEquals(640_000L, afterLongHold.deltaMs)
    }

    @Test
    fun `new press direction change and long input gap restart at base step`() {
        val first = SeekAccelerationPolicy.nextStep(1, 0, 1_000L, SeekAccelerationPolicy.State())!!
        val accelerated = SeekAccelerationPolicy.nextStep(1, 1, 1_500L, first.state)!!
        assertEquals(20_000L, accelerated.deltaMs)

        val changedDirection = SeekAccelerationPolicy.nextStep(-1, 2, 1_600L, accelerated.state)!!
        assertEquals(-10_000L, changedDirection.deltaMs)

        val afterGap = SeekAccelerationPolicy.nextStep(1, 3, 2_400L, accelerated.state)!!
        assertEquals(10_000L, afterGap.deltaMs)

        val newPress = SeekAccelerationPolicy.nextStep(1, 0, 2_500L, afterGap.state)!!
        assertEquals(10_000L, newPress.deltaMs)
    }

    @Test
    fun `pending target accumulates from intended target and clamps at VOD bounds`() {
        val pending = PendingSeekTargetState()
        assertEquals(130_000L, pending.addDelta(120_000L, 300_000L, 10_000L))
        assertEquals(150_000L, pending.addDelta(120_000L, 300_000L, 20_000L))
        assertEquals(0L, pending.addDelta(120_000L, 300_000L, -200_000L))
        assertEquals(1_000L, pending.addDelta(8_000L, 300_000L, -200_000L, minimumTargetMs = 1_000L))
        val lowStart = PendingSeekTargetState()
        assertEquals(500L, lowStart.addDelta(500L, 300_000L, -200_000L, minimumTargetMs = 500L))
        assertEquals(299_000L, pending.addDelta(120_000L, 300_000L, Long.MAX_VALUE))
    }

    @Test
    fun `unknown or transient duration cannot overwrite an accumulated target`() {
        val pending = PendingSeekTargetState()
        assertEquals(30_000L, pending.addDelta(20_000L, 120_000L, 10_000L))

        assertNull(pending.addDelta(0L, 0L, 10_000L))
        assertNull(pending.addDelta(0L, 999L, 10_000L))
        assertEquals(30_000L, pending.targetOrNull())
    }

    @Test
    fun `absolute seek replaces pending intent and stays inside the VOD duration`() {
        val pending = PendingSeekTargetState()
        pending.addDelta(10_000L, 60_000L, 20_000L)
        assertEquals(59_000L, pending.setTarget(Long.MAX_VALUE, 60_000L))
        assertEquals(59_000L, pending.targetOrNull())
        assertEquals(0L, pending.setTarget(Long.MIN_VALUE, 60_000L))
    }

    @Test
    fun `acknowledgement clears only when engine reaches the submitted target`() {
        val pending = PendingSeekTargetState()
        pending.addDelta(120_000L, 300_000L, 20_000L)

        assertFalse(pending.acknowledgeIfReached(0L, 2_000L))
        assertEquals(140_000L, pending.targetOrNull())
        assertTrue(pending.acknowledgeIfReached(139_000L, 2_000L))
        assertNull(pending.targetOrNull())
    }

    @Test
    fun `heuristic recovery waits during a held seek and for fifteen seconds after release`() {
        assertTrue(SeekRecoveryGracePolicy.shouldDefer(nowMs = 20_000L, lastSeekAtMs = -1L, keyHeld = true))
        assertTrue(SeekRecoveryGracePolicy.shouldDefer(nowMs = 24_999L, lastSeekAtMs = 10_000L, keyHeld = false))
        assertFalse(SeekRecoveryGracePolicy.shouldDefer(nowMs = 25_000L, lastSeekAtMs = 10_000L, keyHeld = false))
        assertTrue(SeekRecoveryGracePolicy.shouldDefer(nowMs = 9_999L, lastSeekAtMs = 10_000L, keyHeld = false))
    }

    @Test
    fun `recovery keeps the pending target even when engine position is zero`() {
        assertEquals(125_000L, SeekRecoveryGracePolicy.resumePosition(125_000L, 0L))
        assertEquals(0L, SeekRecoveryGracePolicy.resumePosition(0L, 75_000L))
    }

    @Test
    fun `an engine failure during ordinary vod seek retries once on the selected source`() {
        assertTrue(SeekFailureRecoveryPolicy.shouldRetrySameSource(false, false, 125_000L, false))
        assertFalse(SeekFailureRecoveryPolicy.shouldRetrySameSource(false, false, 125_000L, true))
        assertFalse(SeekFailureRecoveryPolicy.shouldRetrySameSource(false, false, null, false))
        assertFalse(SeekFailureRecoveryPolicy.shouldRetrySameSource(true, false, 125_000L, false))
        assertFalse(SeekFailureRecoveryPolicy.shouldRetrySameSource(false, true, 125_000L, false))
    }
}

package com.qiubo.optimaltv

import com.qiubo.optimaltv.playback.LiveWindowPolicy
import org.junit.Assert.assertEquals
import org.junit.Test

class LiveWindowPolicyTest {
    @Test fun smallFootballWindowKeepsADeeperSafetyMargin() {
        assertEquals(3_600L, LiveWindowPolicy.targetOffsetMs(6_000L))
        assertEquals(7_200L, LiveWindowPolicy.targetOffsetMs(12_000L))
    }

    @Test fun unknownOrLargeWindowKeepsLegacyTarget() {
        assertEquals(15_000L, LiveWindowPolicy.targetOffsetMs(0L))
        assertEquals(15_000L, LiveWindowPolicy.targetOffsetMs(30_000L))
    }
}

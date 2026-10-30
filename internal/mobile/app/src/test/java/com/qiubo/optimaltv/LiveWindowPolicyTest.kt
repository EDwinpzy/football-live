package com.qiubo.optimaltv

import com.qiubo.optimaltv.playback.LiveWindowPolicy
import org.junit.Assert.assertEquals
import org.junit.Test

class LiveWindowPolicyTest {
    @Test fun longSegmentsStayInsideFiveSecondBudget() {
        assertEquals(4_000L, LiveWindowPolicy.targetOffsetMs(32_400L, 5_400L))
        assertEquals(5_000L, LiveWindowPolicy.maxOffsetMs(32_400L, 5_400L))
        assertEquals(4_000L, LiveWindowPolicy.targetOffsetMs(30_000L, 2_000L))
    }

    @Test fun smallWindowAndUnknownWindowRemainBounded() {
        assertEquals(3_600L, LiveWindowPolicy.targetOffsetMs(6_000L))
        assertEquals(4_000L, LiveWindowPolicy.targetOffsetMs(12_000L))
        assertEquals(3_000L, LiveWindowPolicy.targetOffsetMs(0L))
        assertEquals(5_000L, LiveWindowPolicy.maxOffsetMs(30_000L))
    }
}

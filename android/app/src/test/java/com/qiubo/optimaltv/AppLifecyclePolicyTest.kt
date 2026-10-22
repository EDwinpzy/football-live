package com.qiubo.optimaltv

import com.qiubo.optimaltv.lifecycle.AppLifecyclePolicy
import com.qiubo.optimaltv.lifecycle.ForegroundDecision
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AppLifecyclePolicyTest {
    @Test fun `ordinary short background refreshes without forcing live renewal`() {
        val decision = AppLifecyclePolicy.onForeground(backgroundedAtMs = 1_000, nowMs = 20_000, isLive = true)
        assertEquals(ForegroundDecision.REFRESH, decision)
    }

    @Test fun `live playback renews after thirty seconds in background`() {
        val decision = AppLifecyclePolicy.onForeground(backgroundedAtMs = 1_000, nowMs = 31_000, isLive = true)
        assertEquals(ForegroundDecision.REFRESH_AND_RENEW_LIVE, decision)
    }

    @Test fun `non-live lifecycle never renews live playback and explicit exit is separate from background`() {
        assertEquals(ForegroundDecision.REFRESH,
            AppLifecyclePolicy.onForeground(backgroundedAtMs = 1_000, nowMs = 90_000, isLive = false))
        assertFalse(AppLifecyclePolicy.isExplicitExit(false))
        assertTrue(AppLifecyclePolicy.isExplicitExit(true))
    }
}


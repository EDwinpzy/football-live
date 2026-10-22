package com.qiubo.optimaltv.apkupdate

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ApkCheckGateTest {
    @Test fun coldStartupChecksOnce() {
        val gate = ApkCheckGate()
        assertTrue(gate.onForegroundChanged(true))
        assertFalse(gate.onForegroundChanged(true))
    }

    @Test fun reopeningSameProcessChecksAgain() {
        val gate = ApkCheckGate()
        repeat(3) {
            assertTrue(gate.onForegroundChanged(true))
            assertTrue(gate.tryStartCheck())
            gate.finishCheck()
            assertFalse(gate.onForegroundChanged(false))
        }
    }

    @Test fun repeatedBackgroundCallbacksDoNotTriggerChecks() {
        val gate = ApkCheckGate()
        assertFalse(gate.onForegroundChanged(false))
        assertFalse(gate.onForegroundChanged(false))
        assertTrue(gate.onForegroundChanged(true))
    }

    @Test fun manualAndAutomaticChecksCannotOverlap() {
        val gate = ApkCheckGate()
        assertTrue(gate.tryStartCheck())
        assertFalse(gate.tryStartCheck())
        gate.finishCheck()
        assertTrue(gate.tryStartCheck())
    }

    @Test fun failedCheckCanBeRetriedOnNextOpening() {
        val gate = ApkCheckGate()
        assertTrue(gate.onForegroundChanged(true))
        assertTrue(gate.tryStartCheck())
        gate.finishCheck()
        gate.onForegroundChanged(false)
        assertTrue(gate.onForegroundChanged(true))
        assertTrue(gate.tryStartCheck())
    }
}

package com.qiubo.optimaltv.data.repo

import org.junit.Assert.assertEquals
import org.junit.Test

class LiveSourcePolicyTest {
    @Test fun retainsRememberedSourceWhileAvailable() {
        assertEquals("source-b", LiveSourcePolicy.preferred("source-b", listOf("source-a", "source-b")))
    }

    @Test fun fallsBackToFirstAvailableSourceWhenRememberedSourceDisappears() {
        assertEquals("source-a", LiveSourcePolicy.preferred("removed", listOf("source-a", "source-b")))
    }
}

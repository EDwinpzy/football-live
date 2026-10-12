package com.qiubo.optimaltv.playback

import org.junit.Assert.assertEquals
import org.junit.Test

class VlcRelaySessionTest {
    private val session = "a".repeat(32)

    @Test fun addsSessionOnlyToLocalRelay() {
        assertEquals("http://127.0.0.1:8090/api/relay?u=test&vlc_session=$session",
            VlcRelaySession.playbackUrl("http://127.0.0.1:8090/api/relay?u=test#fragment", session))
        assertEquals("http://localhost:8090/api/relay?vlc_session=$session",
            VlcRelaySession.playbackUrl("http://localhost:8090/api/relay", session))
    }

    @Test fun leavesDirectAndUnrelatedUrlsUnchanged() {
        listOf("https://example.com/live.m3u8", "http://127.0.0.1:8090/api/relayts?u=test", "bad url")
            .forEach { assertEquals(it, VlcRelaySession.playbackUrl(it, session)) }
        val local = "http://127.0.0.1:8090/api/relay?u=test"
        assertEquals(local, VlcRelaySession.playbackUrl(local, "invalid"))
    }
}

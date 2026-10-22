package com.qiubo.optimaltv.data.analytics

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Contracts for live playback telemetry and signed source URL privacy. */
class PlaybackTelemetryTest {

    @Test
    fun redactDropsSignedUrlQuery() {
        val signedUrl = "https://cdn.example.com/live/match.m3u8?sign=secret&token=private"

        assertEquals("https://cdn.example.com/live/match.m3u8", PlaybackTelemetry.redact(signedUrl))
        assertEquals("-", PlaybackTelemetry.redact("  "))
    }

    @Test
    fun sourceReferencesAreHashedBeforeRecording() = withSilentSink {
        val signedUrl = "https://cdn.example.com/live/match.m3u8?sign=secret&token=private"
        val session = PlaybackTelemetry.newSession("live:match-123")

        session.mark(PlaybackTelemetry.Stage.RESOLVE_START, source="source:primary")
        session.mark(PlaybackTelemetry.Stage.FIRST_FRAME, source=signedUrl)

        val output = (session.recorded().map { it.format() } + session.summary(signedUrl).format()).joinToString("\n")
        assertFalse(output.contains("cdn.example.com"))
        assertFalse(output.contains("secret"))
        assertFalse(output.contains("private"))
        assertEquals(8, session.recorded().last().sourceHash.length)
    }

    @Test
    fun sourceHashIsStableAndDistinguishesSources() {
        val first = PlaybackTelemetry.urlHash("source:primary")

        assertEquals(first, PlaybackTelemetry.urlHash("source:primary"))
        assertNotEquals(first, PlaybackTelemetry.urlHash("source:backup"))
        assertEquals(8, first.length)
        assertFalse(first.contains("source"))
    }

    @Test
    fun unmeasuredSessionStagesUseMinusOneUntilFirstFrame() = withSilentSink {
        val session = PlaybackTelemetry.newSession("live:match-123")

        assertEquals(-1L, session.summary().totalClickToFrameMs)
        session.mark(PlaybackTelemetry.Stage.RESOLVE_START)
        session.mark(PlaybackTelemetry.Stage.PLAYER_PREPARE)
        assertEquals(-1L, session.summary().totalClickToFrameMs)

        session.mark(PlaybackTelemetry.Stage.FIRST_FRAME)
        assertTrue(session.summary().totalClickToFrameMs >= 0L)
    }

    @Test
    fun sensitiveFieldNamesAreRecognized() {
        listOf("play_url", "token", "Authorization", "cookie", "secret_key").forEach { key ->
            assertTrue("expected $key to be treated as sensitive", PlaybackTelemetry.isForbiddenKey(key))
        }
        assertFalse(PlaybackTelemetry.isForbiddenKey("sourceId"))
    }

    @Test
    fun stagesHaveUniqueWireNames() {
        val names = PlaybackTelemetry.Stage.values().map { it.wire }

        assertEquals(names.size, names.toSet().size)
        assertTrue("resolve_start" in names)
        assertTrue("first_frame" in names)
        assertTrue("engine_fallback" in names)
    }

    @Test
    fun classifiesLivePlaybackFailuresForRecovery() {
        assertEquals(PlaybackTelemetry.ErrorClass.NETWORK, PlaybackTelemetry.classify("connection timed out"))
        assertEquals(PlaybackTelemetry.ErrorClass.DECODE, PlaybackTelemetry.classify("decoder failed"))
        assertEquals(PlaybackTelemetry.ErrorClass.SERVER, PlaybackTelemetry.classify("HTTP 403 forbidden"))
    }

    private inline fun withSilentSink(block: () -> Unit) {
        val previousSink = PlaybackTelemetry.sink
        PlaybackTelemetry.sink = {}
        try {
            block()
        } finally {
            PlaybackTelemetry.sink = previousSink
        }
    }
}

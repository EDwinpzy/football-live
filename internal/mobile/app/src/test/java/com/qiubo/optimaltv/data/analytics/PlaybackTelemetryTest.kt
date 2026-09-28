package com.qiubo.optimaltv.data.analytics

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 埋点隐私契约测试。
 *
 * 硬性红线：**播放 URL、Cookie、签名 token、Authorization 头一律不得进日志**。
 * 这组用例是那道红线的可执行版本——真出线上事故时，CI 会红。
 */
class PlaybackTelemetryTest {

    @Test
    fun stripsPlaybackUrls() {
        assertEquals("", PlaybackTelemetry.sanitize("http://cdn.example.com/live/a.m3u8?sign=abc"))
        assertEquals("", PlaybackTelemetry.sanitize("https://x.y/z.m3u8"))
        assertEquals("", PlaybackTelemetry.sanitize("//relay/api/relay?u=http://a/b"))
    }

    @Test
    fun stripsCredentialShapedValues() {
        assertEquals("", PlaybackTelemetry.sanitize("Authorization: Bearer eyJhbGciOi"))
        assertEquals("", PlaybackTelemetry.sanitize("Cookie: uid=123"))
        assertEquals("", PlaybackTelemetry.sanitize("token=abcdef"))
        assertEquals("", PlaybackTelemetry.sanitize("sign=deadbeef"))
        assertEquals("", PlaybackTelemetry.sanitize("secret_key=zzz"))
        assertEquals("", PlaybackTelemetry.sanitize("password=hunter2"))
    }

    @Test
    fun keepsStableLineReferences() {
        // 线路引用是站点稳定标识（sourceId:contentId:epIndex），不含签名，必须保留——
        // 否则埋点就失去了「哪条源出的问题」这个唯一有用的维度。
        assertEquals("hhkan", PlaybackTelemetry.sanitize("hhkan"))
        assertEquals("vod-1292052", PlaybackTelemetry.sanitize("vod-1292052"))
        assertEquals("media3", PlaybackTelemetry.sanitize("media3"))
    }

    @Test
    fun blankStaysBlank() {
        assertEquals("", PlaybackTelemetry.sanitize(""))
        assertEquals("", PlaybackTelemetry.sanitize("   "))
    }

    @Test
    fun trimsAndCapsLength() {
        assertEquals("abc", PlaybackTelemetry.sanitize("  abc  "))
        val long = "x".repeat(200)
        assertEquals(64, PlaybackTelemetry.sanitize(long).length)
    }

    @Test
    fun sessionEndToEndIsNullUntilFirstFrame() {
        val session = PlaybackTelemetry.Session("douban:1291546")
        // 未出画面不能报 0——0 会把均值污染成「秒开」。
        assertNull(session.totalClickToFrameMs())
        session.emit(PlaybackTelemetry.Phase.RESOLVE_START)
        session.emit(PlaybackTelemetry.Phase.PLAYER_PREPARE)
        assertNull(session.totalClickToFrameMs())
        session.emit(PlaybackTelemetry.Phase.FIRST_FRAME)
        assertTrue((session.totalClickToFrameMs() ?: -1L) >= 0L)
    }

    @Test
    fun countersAccumulateAcrossCalls() {
        val counter = PlaybackTelemetry.Counter()
        assertEquals(0L, counter.get("k"))
        counter.inc("k")
        counter.inc("k", 4L)
        assertEquals(5L, counter.get("k"))
        assertEquals(0L, counter.get("other"))
    }

    @Test
    fun everyPhaseHasAStableWireName() {
        val names = PlaybackTelemetry.Phase.values().map { it.wire }
        assertEquals(names.size, names.toSet().size)
        // 契约里约定的 9 个事件必须齐全
        for (expected in listOf(
            "resolve_start", "resolve_first_source", "player_prepare", "first_frame",
            "buffer_start", "buffer_end", "auto_switch", "engine_fallback", "fatal",
        )) {
            assertTrue("缺少埋点事件 $expected", expected in names)
        }
    }

    @Test
    fun sanitizerIsTheLastLineOfDefenceForEveryField() {
        // 模拟调用方把所有字段都塞进 URL 的最坏情况：全部应被抹成空串。
        val nasty = "https://cdn.example.com/x.m3u8?sign=deadbeef&token=xyz"
        assertEquals("", PlaybackTelemetry.sanitize(nasty))
        assertFalse(PlaybackTelemetry.sanitize(nasty).contains("example.com"))
    }
}

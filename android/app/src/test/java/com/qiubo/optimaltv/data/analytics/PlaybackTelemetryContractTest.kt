package com.qiubo.optimaltv.data.analytics

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [PlaybackTelemetry] 契约测试。
 *
 * 最重要的一条是**隐私红线**：埋点绝不能把播放地址、签名 token、Cookie、Authorization
 * 头写进日志。点播直链普遍带分钟级签名，日志又会经 OtvLog 持久化到
 * files/logs/otv.log，一旦漏出去等于泄漏可播放凭证。所以这里用真实的带签名 URL 做
 * 逐条断言，而不是只测「有没有打点」。
 */
class PlaybackTelemetryContractTest {

    /** 一条典型的带签名点播直链。 */
    private val playUrl = "https://cdn.example.com/hls/movie/abc123/index.m3u8"
    private val signed = playUrl + "?sign=SECRETSIGNATURE&t=1758000000&uid=998877"

    @Test fun `redact strips the query so a signed url can never reach the log`() {
        val redacted = PlaybackTelemetry.redact(signed)
        assertFalse("签名参数不能出现在日志里", redacted.contains("sign"))
        assertFalse("时间戳 token 不能出现在日志里", redacted.contains("1758000000"))
        assertFalse("uid 不能出现在日志里", redacted.contains("998877"))
        assertTrue("应保留 scheme+host+path 便于排查", redacted.startsWith(playUrl))
    }

    @Test fun `redact handles blank input and urls without a query`() {
        assertEquals("-", PlaybackTelemetry.redact(""))
        assertEquals("-", PlaybackTelemetry.redact("   "))
        assertEquals(playUrl, PlaybackTelemetry.redact(playUrl))
    }

    @Test fun `an event line never contains the url cookie or token`() {
        val session = PlaybackTelemetry.newSession("t-redact")
        session.mark(
            PlaybackTelemetry.Stage.PLAYER_PREPARE,
            source = signed,
            detail = "engine=media3",
        )
        val line = session.recorded().last().format()
        listOf("SECRETSIGNATURE", "1758000000", "998877", "sign=", "?").forEach { secret ->
            assertFalse("埋点行泄漏了 '$secret'：$line", line.contains(secret))
        }
        // 地址本身也不该出现——只允许短哈希。
        assertFalse("埋点行不得出现地址：$line", line.contains("cdn.example.com"))
    }

    @Test fun `the url hash is stable but not reversible to the url`() {
        val a = PlaybackTelemetry.urlHash(playUrl)
        assertEquals("同一地址必须算出同一哈希，否则跨日志无法关联", a, PlaybackTelemetry.urlHash(playUrl))
        assertFalse("哈希里不能出现地址片段", a.contains("cdn"))
        assertNotEquals("不同地址必须算出不同哈希", a, PlaybackTelemetry.urlHash("https://other/x.m3u8"))
        assertEquals("空地址走占位符", "-", PlaybackTelemetry.urlHash(""))
    }

    @Test fun `every stage maps to the wire name the analytics pipeline expects`() {
        val expected = mapOf(
            PlaybackTelemetry.Stage.RESOLVE_START to "resolve_start",
            PlaybackTelemetry.Stage.RESOLVE_FIRST_SOURCE to "resolve_first_source",
            PlaybackTelemetry.Stage.PLAYER_PREPARE to "player_prepare",
            PlaybackTelemetry.Stage.FIRST_FRAME to "first_frame",
            PlaybackTelemetry.Stage.BUFFER_START to "buffer_start",
            PlaybackTelemetry.Stage.BUFFER_END to "buffer_end",
            PlaybackTelemetry.Stage.AUTO_SWITCH to "auto_switch",
            PlaybackTelemetry.Stage.ENGINE_FALLBACK to "engine_fallback",
            PlaybackTelemetry.Stage.FATAL to "fatal",
        )
        assertEquals("阶段数与契约不一致", expected.size, PlaybackTelemetry.Stage.values().size)
        expected.forEach { (stage, wire) -> assertEquals(wire, stage.wire) }
    }

    @Test fun `a session reports the five key durations`() {
        val session = PlaybackTelemetry.newSession("t-summary")
        session.mark(PlaybackTelemetry.Stage.RESOLVE_START)
        session.mark(PlaybackTelemetry.Stage.RESOLVE_FIRST_SOURCE)
        session.mark(PlaybackTelemetry.Stage.PLAYER_PREPARE)
        session.mark(PlaybackTelemetry.Stage.FIRST_FRAME, source = playUrl)
        val summary = session.summary(playUrl)
        listOf(summary.detailMs, summary.resolveMs, summary.prepareMs,
            summary.firstFrameMs, summary.totalClickToFrameMs).forEach {
            assertTrue("已量到的耗时不应为负：$summary", it >= 0L)
        }
        assertEquals("total_click_to_frame 与 first_frame 同源", summary.firstFrameMs, summary.totalClickToFrameMs)
        assertFalse("结算行不应泄漏地址", summary.format().contains("cdn"))
        listOf("detail", "resolve", "prepare", "first_frame", "total_click_to_frame").forEach { key ->
            assertTrue("结算行缺字段 $key：${summary.format()}", summary.format().contains(key))
        }
    }

    @Test fun `an unmeasured duration is -1 rather than a fabricated estimate`() {
        val session = PlaybackTelemetry.newSession("t-partial")
        val summary = session.summary()
        assertEquals("未量到的段落必须是 -1（未知），不能填 0", -1L, summary.resolveMs)
        assertEquals(-1L, summary.prepareMs)
        assertEquals(-1L, summary.firstFrameMs)
        assertEquals(-1L, summary.totalClickToFrameMs)
        assertEquals(-1L, summary.detailMs)
    }

    @Test fun `elapsed time never goes backwards across marks`() {
        val session = PlaybackTelemetry.newSession("t-order")
        session.mark(PlaybackTelemetry.Stage.RESOLVE_START)
        val first = session.elapsedOf(PlaybackTelemetry.Stage.RESOLVE_START)!!
        Thread.sleep(15)
        session.mark(PlaybackTelemetry.Stage.FIRST_FRAME)
        val second = session.elapsedOf(PlaybackTelemetry.Stage.FIRST_FRAME)!!
        assertTrue("单调时钟必须单调递增：$first -> $second", second >= first)
    }

    @Test fun `network errors are classified as NETWORK so the player switches source`() {
        listOf(
            "java.net.SocketTimeoutException: timeout",
            "Unable to resolve host",
            "Connection reset by peer",
            "Network is unreachable",
        ).forEach { raw ->
            assertEquals(
                "「$raw」应判为网络类（换源有用）",
                PlaybackTelemetry.ErrorClass.NETWORK,
                PlaybackTelemetry.classify(raw),
            )
        }
    }

    @Test fun `decode errors are classified as DECODE so the player changes engine`() {
        listOf(
            "MediaCodec video decoder error",
            "DECODING_FAILED",
            "no frame rendered after vout setup",
            // Media3 真实上报的解码器错误码（PlaybackException.ERROR_CODE_DECODING_FAILED）。
            // 「decoding」里不含「decode」，旧关键词表漏判 → 判成 UNKNOWN →
            // 既不换源也不换引擎，用户只能看着黑屏。
            "PlaybackException ERROR_CODE_DECODING_FAILED",
            "com.google.android.exoplayer2.ExoPlaybackException: DecoderInitializationException",
        ).forEach { raw ->
            assertEquals(
                "「$raw」应判为解码类（该换引擎）",
                PlaybackTelemetry.ErrorClass.DECODE,
                PlaybackTelemetry.classify(raw),
            )
        }
    }

    @Test fun `a throwing sink never breaks playback`() {
        // 埋点「永不影响播放」是硬约束：sink 里 logcat/文件写盘抛异常时，
        // mark() 必须吞掉，否则一次日志故障会让点播放直接崩在主线程。
        val original = PlaybackTelemetry.sink
        try {
            PlaybackTelemetry.sink = { error("sink exploded") }
            val session = PlaybackTelemetry.newSession("t-sink")
            // 不抛异常即通过。
            session.mark(PlaybackTelemetry.Stage.RESOLVE_START)
            session.mark(PlaybackTelemetry.Stage.FIRST_FRAME, source = playUrl)
            assertEquals(
                listOf(
                    PlaybackTelemetry.Stage.RESOLVE_START,
                    PlaybackTelemetry.Stage.FIRST_FRAME,
                ),
                session.recorded().map { it.stage },
            )
            // 计时链不能因为 sink 抛异常而断掉。
            assertEquals(2, session.recorded().size)
            assertTrue(session.elapsedOf(PlaybackTelemetry.Stage.FIRST_FRAME) != null)
        } finally {
            PlaybackTelemetry.sink = original
        }
    }

    @Test fun `the default sink survives a plain JVM environment`() {
        // 默认 sink 同时走 android.util.Log 与 OtvLog。JVM 单测里 Log 是未 mock 的桩，
        // 直接调会抛 `Method i in android.util.Log not mocked`——契约测试会整个红掉，
        // 于是「埋点不能泄漏地址」这条最要紧的断言根本跑不到。
        // 这里直接调真实默认实现（不替换 sink），锁住「自带兜底、不抛」这个行为。
        val outcome = runCatching { PlaybackTelemetry.defaultSink("[probe] first_frame") }
        assertTrue(
            "默认 sink 在未 mock 的 JVM 环境下不得抛异常（实测曾抛 Log not mocked）：${outcome.exceptionOrNull()}",
            outcome.isSuccess,
        )
    }

    @Test fun `decode classification wins when a message carries both signals`() {
        // 判成 NETWORK 会去换线路，可内容还是同样的编码，换一百条线也解不出来。
        assertEquals(
            PlaybackTelemetry.ErrorClass.DECODE,
            PlaybackTelemetry.classify("decoder stalled while socket timeout counting"),
        )
    }

    @Test fun `server rejections and blanks are classified distinctly`() {
        assertEquals(
            PlaybackTelemetry.ErrorClass.SERVER,
            PlaybackTelemetry.classify("HTTP 403 Forbidden"),
        )
        assertEquals(PlaybackTelemetry.ErrorClass.NONE, PlaybackTelemetry.classify(""))
        assertEquals(PlaybackTelemetry.ErrorClass.NONE, PlaybackTelemetry.classify("   "))
        assertEquals(PlaybackTelemetry.ErrorClass.UNKNOWN, PlaybackTelemetry.classify("something odd"))
    }

    @Test fun `sensitive field names are recognised as forbidden`() {
        listOf("url", "playUrl", "PLAY_URL", "token", "sign", "signature", "cookie",
            "Authorization", "password", "jwt").forEach { key ->
            assertTrue("「$key」应被识别为敏感字段", PlaybackTelemetry.isForbiddenKey(key))
        }
        listOf("source_id", "elapsed", "errorClass", "mytv_id", "episode").forEach { key ->
            assertFalse("「$key」不是敏感字段", PlaybackTelemetry.isForbiddenKey(key))
        }
    }

    @Test fun `recorded events are retained in order for offline analysis`() {
        val session = PlaybackTelemetry.newSession("t-records")
        session.mark(PlaybackTelemetry.Stage.RESOLVE_START)
        session.mark(PlaybackTelemetry.Stage.RESOLVE_FIRST_SOURCE, source = playUrl)
        session.mark(PlaybackTelemetry.Stage.PLAYER_PREPARE, source = playUrl)
        session.mark(PlaybackTelemetry.Stage.FIRST_FRAME, source = playUrl)
        assertEquals(
            listOf(
                PlaybackTelemetry.Stage.RESOLVE_START,
                PlaybackTelemetry.Stage.RESOLVE_FIRST_SOURCE,
                PlaybackTelemetry.Stage.PLAYER_PREPARE,
                PlaybackTelemetry.Stage.FIRST_FRAME,
            ),
            session.recorded().map { it.stage },
        )
        assertTrue(session.recorded().none { it.format().contains("cdn") })
    }
}

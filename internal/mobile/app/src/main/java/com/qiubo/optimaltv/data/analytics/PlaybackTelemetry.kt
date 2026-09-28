package com.qiubo.optimaltv.data.analytics

import com.qiubo.optimaltv.OtvLog
import java.util.concurrent.atomic.AtomicLong

/**
 * 播放链路埋点。
 *
 * 记录 resolve / prepare / 首帧 / 缓冲 / 自动换源 / 引擎降级 的阶段耗时，用来回答
 * 「点一下到出画面到底卡在哪一段」。
 *
 * 隐私红线（硬性）：**任何播放 URL、Cookie、签名 token、Authorization 头都不得进日志**。
 * 因此本类根本不接收 URL 字符串——只接收线路引用（source_id / content_id）和枚举阶段名。
 * [sanitize] 是最后一道防线：即使调用方误把 URL 传进 [note] 也会被整段抹掉。
 */
object PlaybackTelemetry {

    enum class Phase(val wire: String) {
        RESOLVE_START("resolve_start"),
        RESOLVE_FIRST_SOURCE("resolve_first_source"),
        PLAYER_PREPARE("player_prepare"),
        FIRST_FRAME("first_frame"),
        BUFFER_START("buffer_start"),
        BUFFER_END("buffer_end"),
        AUTO_SWITCH("auto_switch"),
        ENGINE_FALLBACK("engine_fallback"),
        FATAL("fatal"),
    }

    /** 一次「点击播放 → 出画面」的完整计时上下文。 */
    class Session(val vodId: String) {
        private val clickAt = System.currentTimeMillis()
        private val marks = LinkedHashMap<String, Long>()

        fun markDetailStart() {
            marks["detail_start"] = System.currentTimeMillis()
        }

        fun markDetailDone() {
            marks["detail_end"] = System.currentTimeMillis()
        }

        fun emit(phase: Phase, sourceId: String = "", contentId: String = "", note: String = "") {
            val now = System.currentTimeMillis()
            marks[phase.wire] = now
            val parts = ArrayList<String>(12)
            parts.add("pb")
            if (vodId.isNotBlank()) parts.add("vod=" + sanitize(vodId))
            if (sourceId.isNotBlank()) parts.add("src=" + sanitize(sourceId))
            if (contentId.isNotBlank()) parts.add("cid=" + sanitize(contentId))
            for ((key, value) in marks) parts.add(key + "=" + (value - clickAt))
            if (note.isNotBlank()) parts.add("n=" + sanitize(note))
            OtvLog.i(parts.joinToString(" "))
        }

        /** 端到端：点击 → 首帧。未出画面时返回 null（不臆造 0，避免污染均值）。 */
        fun totalClickToFrameMs(): Long? =
            marks[Phase.FIRST_FRAME.wire]?.let { it - clickAt }
    }

    private val SENSITIVE = listOf(
        "http", "//", "token", "cookie", "auth", "secret", "password", "sign=", "key=",
        "?", "&", "=",
    )

    /**
     * 抹掉任何看起来像地址 / 凭证的片段。埋点只应承载阶段名与稳定 id，
     * 一旦出现 http、//host、token、key、cookie 之类就整段丢弃。
     */
    fun sanitize(raw: String): String {
        val value = raw.trim()
        if (value.isEmpty()) return ""
        val lower = value.lowercase()
        if (SENSITIVE.any { lower.contains(it) }) return ""
        return value.take(64)
    }

    /** 供单测与上层复用的一次性计数器（避免每个 VM 各自拼字符串）。 */
    class Counter {
        private val counters = java.util.concurrent.ConcurrentHashMap<String, AtomicLong>()

        fun inc(key: String, by: Long = 1L): Long =
            counters.computeIfAbsent(key) { AtomicLong() }.addAndGet(by)

        fun get(key: String): Long = counters[key]?.get() ?: 0L
    }

    /** 进程级共享计数器：取源分层命中次数等全局统计。 */
    val counters = Counter()
}

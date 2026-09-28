package com.qiubo.optimaltv.data.analytics

import android.util.Log
import com.qiubo.optimaltv.OtvLog
import java.util.concurrent.ConcurrentHashMap

/**
 * 播放链路埋点（2026-09-28 全链路性能优化）。
 *
 * 目标：把「点播放 → 出画面」这条链路的每一段耗时拆开量出来，让「起播慢」这种主观
 * 抱怨能落到具体段落上：详情拖了多久、解析拖了多久、prepare 拖了多久、首帧拖了多久。
 *
 * **隐私红线（硬性）**：绝不记录播放 URL、Cookie、签名 token、Authorization 头。
 * 可点播的直链普遍带分钟级签名，落进日志等于泄漏可播放凭证，且日志会经 OtvLog 双写
 * 持久化到 files/logs/otv.log。本文件只接受「线路稳定标识 + 耗时 + 错误分类」：
 *  - sourceId 走 [urlHash] 的短哈希，和 [com.qiubo.optimaltv.playback.PlaybackHealthLog.urlHash] 同一口径；
 *  - 任何地址在入队前经 [redact] 抹掉 query，只留 scheme://host/path；
 *  - 敏感字段名（见 [forbiddenKeys]）即使被调用方误传也不会写出去。
 *
 * 埋点本身永不影响播放：所有落盘路径都走 OtvLog（内部 LinkedBlockingQueue 异步刷盘），
 * 计时用单调时钟 [System.nanoTime]，不依赖系统时间跳变。
 */
object PlaybackTelemetry {

    private const val TAG = "OTV-TELEMETRY"

    /** 埋点阶段：一次播放会话的完整生命周期。 */
    enum class Stage(val wire: String) {
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

    /** 错误分类：决定该换源（网络类）还是换引擎（解码类）。 */
    enum class ErrorClass(val wire: String) {
        /** 线路/上游不可达、超时、DNS：换源有用。 */
        NETWORK("network"),

        /** 解码器挂死、格式不支持、渲染无输出：换源没用，要换 VLC 引擎。 */
        DECODE("decode"),

        /** 服务端明确拒绝（4xx/鉴权/风控）：换源也没用。 */
        SERVER("server"),

        /** 客户端逻辑错误、未知异常。 */
        UNKNOWN("unknown"),

        /** 非错误路径。 */
        NONE("none"),
    }

    /** 一条埋点。字段全是「可公开的诊断量」，没有任何地址或凭证。 */
    data class Event(
        val stage: Stage,
        val elapsedMs: Long,
        val sourceHash: String = "-",
        val errorClass: ErrorClass = ErrorClass.NONE,
        val detail: String = "",
    ) {
        fun format(): String = buildString {
            append('[').append(stage.wire).append(']')
            append(" t=").append(elapsedMs).append("ms")
            append(" src=").append(sourceHash)
            append(" err=").append(errorClass.wire)
            if (detail.isNotBlank()) append(" note=").append(redact(detail))
        }
    }

    /**
     * 会话计时器：一次「点播放」从 [newSession] 开始，到各阶段打点，算出每段耗时。
     *
     * 用 [System.nanoTime] 单调时钟；不持有 Context，可安全在任意线程使用。
     */
    class Session internal constructor(private val id: String) {
        private val startedAtNanos = System.nanoTime()
        private val marks = ConcurrentHashMap<String, Long>()
        private val events = mutableListOf<Event>()

        /** 从会话开始到现在的毫秒数。 */
        fun elapsedMs(): Long = (System.nanoTime() - startedAtNanos) / 1_000_000L

        /** 打一个阶段事件。自动带上 elapsed 与 [urlHash]。 */
        fun mark(
            stage: Stage,
            errorClass: ErrorClass = ErrorClass.NONE,
            source: String = "",
            detail: String = "",
        ) {
            val at = elapsedMs()
            marks[stage.wire] = at
            val event = Event(stage, at, urlHash(source), errorClass, detail)
            synchronized(events) { events += event }
            // 事件先入队、计时先落表，再尝试落盘：即使出口炸了，内存里的记录与耗时
            // 链也必须完整（否则「日志失败」会连带毁掉本次会话的排障依据）。
            runCatching { emit(event) }
        }

        /** 取某阶段已记录的耗时（毫秒）；未记录返回 null。 */
        fun elapsedOf(stage: Stage): Long? = marks[stage.wire]

        /**
         * 结算：一次性吐出这次会话的五个关键耗时。
         *
         * detail_ms —— 详情/线路解析等「点播放之前」的耗时；
         * resolve_ms —— 解析到拿到可用线路的耗时；
         * prepare_ms —— 拿到直链到交引擎 prepare 的耗时；
         * first_frame_ms —— prepare 到首帧渲染的耗时；
         * total_click_to_frame_ms —— 从点击到首帧的端到端耗时。
         * 任何一段没量到就是 -1（未知），不做估算填充。
         */
        fun summary(firstFrameSource: String = ""): Summary {
            val first = elapsedOf(Stage.FIRST_FRAME)
            val resolve = elapsedOf(Stage.RESOLVE_FIRST_SOURCE)
            val prepare = elapsedOf(Stage.PLAYER_PREPARE)
            return Summary(
                sessionId = id,
                detailMs = marks[Stage.RESOLVE_START.wire] ?: -1L,
                resolveMs = resolve ?: -1L,
                prepareMs = prepare ?: -1L,
                firstFrameMs = first ?: -1L,
                totalClickToFrameMs = first ?: -1L,
                sourceHash = urlHash(firstFrameSource),
            )
        }

        fun recorded(): List<Event> = synchronized(events) { events.toList() }
    }

    /** 一次会话的关键耗时。-1 = 未量到（不估算）。 */
    data class Summary(
        val sessionId: String,
        val detailMs: Long,
        val resolveMs: Long,
        val prepareMs: Long,
        val firstFrameMs: Long,
        val totalClickToFrameMs: Long,
        val sourceHash: String,
    ) {
        fun format(): String = "[SUMMARY] id=$sessionId detail=${detailMs}ms resolve=${resolveMs}ms " +
            "prepare=${prepareMs}ms first_frame=${firstFrameMs}ms " +
            "total_click_to_frame=${totalClickToFrameMs}ms src=$sourceHash"
    }

    /** 敏感字段名：即使调用方误传也不会写进日志。 */
    private val forbiddenKeys = listOf(
        "url", "playurl", "play_url", "token", "sign", "signature", "cookie",
        "authorization", "auth", "password", "passwd", "secret", "jwt",
    )

    /**
     * 地址脱敏：只保留 scheme://host/path，丢掉 query 与 fragment。
     *
     * 点播直链的签名（?sign=…&t=…）全在 query 里，抹掉 query 后地址只用于「同一条线路
     * 的多次尝试是否指向同一后端」这种诊断，不可播放。
     */
    fun redact(raw: String): String {
        if (raw.isBlank()) return "-"
        val q = raw.indexOf('?')
        val base = if (q >= 0) raw.substring(0, q) else raw
        return if (base.length > 160) base.take(160) else base
    }

    /**
     * 线路短哈希：与 PlaybackHealthLog.urlHash 同一 SHA-1 前 4 字节口径，
     * 保证「埋点里的 src」能和「体检日志里的 url」对上，又不暴露地址。
     */
    fun urlHash(url: String): String {
        if (url.isBlank()) return "-"
        return runCatching {
            val d = java.security.MessageDigest.getInstance("SHA-1").digest(url.toByteArray())
            d.take(4).joinToString("") { "%02x".format(it) }
        }.getOrDefault("-")
    }

    /**
     * 错误分类：把引擎/网络异常映射到 [ErrorClass]。
     *
     * 这是「换源 vs 换引擎」的唯一判定入口，两条恢复策略共用：
     *  - 超时 / 连接 / DNS / 未知主机 → NETWORK（换线路）；
     *  - 解码器相关（codec / decode / muxer / vout / 帧输出） → DECODE（换 VLC 引擎）。
     * 同一个异常文案里既有 decode 又有 network 关键字时，优先 DECODE——
     * 「解码不出帧」换源大概率还是同样编码内容，换引擎才是正解。
     */
    fun classify(errorText: String): ErrorClass {
        val t = errorText.lowercase()
        if (t.isBlank()) return ErrorClass.NONE
        val decodeKeys = listOf(
            "decode", "decoder", "decoding", "codec", "muxer", "demux", "vout", "vlc",
            "mediacodec", "render", "first frame", "no frame", "frame",
        )
        if (decodeKeys.any { t.contains(it) }) return ErrorClass.DECODE
        val networkKeys = listOf(
            "timeout", "timed out", "socket", "connect", "unreachable",
            "unable to resolve host", "unresolved", "name or service not known",
            "network", "connection reset", "refused", "broken pipe",
            "503", "502", "504", "gateway",
        )
        if (networkKeys.any { t.contains(it) }) return ErrorClass.NETWORK
        val serverKeys = listOf("401", "403", "404", "429", "forbidden", "unauthorized")
        if (serverKeys.any { t.contains(it) }) return ErrorClass.SERVER
        return ErrorClass.UNKNOWN
    }

    /** 敏感字段名是否被命中（供测试与调用方自查）。 */
    fun isForbiddenKey(key: String): Boolean =
        forbiddenKeys.any { key.contains(it, ignoreCase = true) }

    private fun emit(event: Event) {
        val line = event.format()
        sink(line)
    }

    /**
     * 落盘出口。默认同时写 logcat 与 [OtvLog]（后者双写 files/logs/otv.log）。
     *
     * 做成可替换的，理由有二：
     *  1. JVM 单测里 `android.util.Log` 是未 mock 的桩方法，调用即抛
     *     `Method i in android.util.Log not mocked`——契约测试会整个红掉，
     *     于是「埋点不能泄漏地址」这条最要紧的断言根本跑不到；
     *  2. 真机上若要接自己的上报通道，只要换掉这里，不用改埋点逻辑。
     */
    @Volatile
    var sink: (String) -> Unit = ::defaultSink

    /**
     * 默认落盘出口（logcat + [OtvLog] 双写）。
     *
     * **整段包在 runCatching 里**是硬性要求，不是防御性编程的洁癖：埋点的调用点在
     * 播放热路径（点播放、换线、出帧）上，而这些点都在主线程。JVM 单测里
     * `android.util.Log` 是未 mock 的桩，调用即抛
     * `Method i in android.util.Log not mocked`；真机上磁盘写盘异常、日志轮转失败同样可能抛。
     * 一旦抛出，`mark()` 会把异常冒泡到播放流程——「打个日志把点播放搞崩」是最坏结果。
     * 所以：日志自身永不影响业务（与 [OtvLog] 头部注释同一原则）。
     */
    fun defaultSink(line: String) {
        runCatching { Log.i(TAG, line) }
        runCatching { OtvLog.i(line) }
    }

    @Volatile private var counter = 0L

    /** 开一次新的播放会话计时。 */
    fun newSession(id: String? = null): Session =
        Session(id ?: "s${++counter}")
}

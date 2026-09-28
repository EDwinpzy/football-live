package com.qiubo.optimaltv.ui.player

/**
 * 起播首帧阶梯（方案 §首帧恢复）。
 *
 * 与 [LongPlaybackHealthPolicy] 分工：这里管「点播放之后多久还没出画面」，
 * 那是网络 + 准备阶段，阈值必须短（用户还盯着黑屏）。
 *
 * 阶段：0–3s 正常等待 → 3–5s 预热备用 → 5–7s 切备用线/换引擎 → 15s 硬超时。
 */
object FirstFrameRecoveryPolicy {

    const val HEALTHY_MS = 3_000L
    const val PREPARE_BACKUP_MS = 5_000L
    const val SWITCH_SOURCE_MS = 7_000L
    const val HARD_TIMEOUT_MS = 15_000L

    enum class Action {
        /** 还在正常范围内，继续等。 */
        WAIT,

        /** 预热备用线路：不动当前会话，只是让下一手更快。 */
        PREPARE_BACKUP,

        /** 换备用线 / 换引擎。 */
        ESCALATE,

        /** 硬超时，放弃并交错误态。 */
        GIVE_UP,
    }

    fun actionFor(elapsedMs: Long): Action = when {
        elapsedMs < HEALTHY_MS -> Action.WAIT
        elapsedMs < PREPARE_BACKUP_MS -> Action.PREPARE_BACKUP
        elapsedMs < SWITCH_SOURCE_MS -> Action.ESCALATE
        elapsedMs < HARD_TIMEOUT_MS -> Action.ESCALATE
        else -> Action.GIVE_UP
    }

    /**
     * 故障归类：**网络类换源，解码类换引擎**。
     *
     * 换源解决「拿不到 / 拉不动」，换引擎解决「拿到了但解不出画面」。
     * 判错方向的代价很高：解码失败反复换源只是把同一条坏线路换着花样重试。
     */
    enum class FailureClass {
        NETWORK,
        DECODER,
        UNKNOWN,
    }

    private val DECODER_MARKERS = listOf(
        "decoding_failed", "decoder", "mediacodec", "vout", "codec",
        "audio", "unsupported", "no suitable", "illegal", "音视频",
    )

    private val NETWORK_MARKERS = listOf(
        "timeout", "timed_out", "unknownhost", "connection", "connect",
        "resolve", "eof", "http_error", "source_error", "io", "tls",
        "error_player_behind", "cleartext",
    )

    fun classify(reason: String): FailureClass {
        val lower = reason.lowercase()
        // 解码特征更特异，先判，避免 "decoder initialization failed by network reset" 误判成网络。
        if (DECODER_MARKERS.any { lower.contains(it) }) return FailureClass.DECODER
        if (NETWORK_MARKERS.any { lower.contains(it) }) return FailureClass.NETWORK
        return FailureClass.UNKNOWN
    }

    /** 该故障应该换线还是换引擎。UNKNOWN 走换源（换引擎成本更高且多数故障是取流问题）。 */
    fun remedyFor(reason: String): Remedy {
        return when (classify(reason)) {
            FailureClass.DECODER -> Remedy.SWITCH_ENGINE
            FailureClass.NETWORK -> Remedy.SWITCH_SOURCE
            FailureClass.UNKNOWN -> Remedy.SWITCH_SOURCE
        }
    }

    enum class Remedy {
        SWITCH_SOURCE,
        SWITCH_ENGINE,
    }
}

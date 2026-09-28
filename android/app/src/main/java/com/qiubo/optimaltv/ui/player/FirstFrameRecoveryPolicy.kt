package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.analytics.PlaybackTelemetry

/**
 * 首帧分级恢复阶梯（2026-09-28）。
 *
 * 旧的首帧看门狗只有一个 15s 硬超时，命中就直接换线/换引擎——用户在电视上盯 15s 黑屏
 * 才知道要换线，且「慢起播」和「起播失败」被当成同一件事。真实体感是渐变的：
 * 0–3s 大部分片源本该出画面，3s 还没出说明这条线偏慢但可能马上好，5s 还没出多半这条
 * 废了，15s 是「用户已经认为坏了」的耐心上限。
 *
 * 阶梯因此分四档，每档对应不同的处置强度：
 *  - 0–3s   WAIT        什么都不做（绝大多数正常起播都在这段内，误判代价最小）；
 *  - 3–5s   PREP_BACKUP 后台预备备用线（不打断当前线，准备好了随时切）；
 *  - 5–7s   SWITCH      切备用线/换引擎（此时仍有机会在 7s 内出画面）；
 *  - 7–15s  SWITCH 之后仍在等 → 升级为 HARD_TIMEOUT 之前的最后机会；
 *  - ≥15s   HARD_TIMEOUT 硬超时，判死并走失败链（弹错误 + 换线重试）。
 *
 * 错误分类决定强度：[PlaybackTelemetry.ErrorClass.DECODE] 类（解码挂死、渲染无输出）
 * 换线路大概率还是同样的编码内容，白白再等一轮——直接跳到 SWITCH 强度；
 * [PlaybackTelemetry.ErrorClass.NETWORK] 类（超时、连不上）先给 PREP_BACKUP 一次机会。
 */
object FirstFrameRecoveryPolicy {

    const val NORMAL_MS = 3_000L
    const val PREP_BACKUP_MS = 5_000L
    const val SWITCH_MS = 7_000L
    const val HARD_TIMEOUT_MS = 15_000L

    /** 首帧等待的处置档位。 */
    enum class Action {
        /** 继续等，不做任何切换。 */
        WAIT,

        /** 后台预备备用线（解析备用线路，不打断当前播放）。 */
        PREP_BACKUP,

        /** 切备用线或换播放引擎。 */
        SWITCH,

        /**
         * 只换引擎：当前线路没有别的可切了（或换线更可能无效），而 media3 的硬解
         * 管线可能已经挂死。
         *
         * 单独拆出这一档是为了修一个真实死分支：旧实现在 PlayerViewModel 里把
         * 「切 libVLC」写成了 `next != null`（存在备用线）分支里的一项，于是
         * **只剩一条线路**时——恰恰是最需要换引擎的时刻——永远走不到它，
         * 一直空耗到 15s 硬超时。把这个决定提到策略层，顺序就不会再被写反。
         */
        ENGINE_FALLBACK,

        /** 硬超时：判死，走失败链（弹错误 + 换线重试）。 */
        HARD_TIMEOUT,

        /** 已经出过帧，后续阶段不再需要本阶梯。 */
        DONE,
    }

    /**
     * 按「已等待毫秒数」决定动作。
     *
     * @param elapsedMs 从 prepare 到现在的毫秒数。
     * @param hasBackup 是否存在可用备用线（没有备用线时 PREP_BACKUP 无意义，直接 SWITCH）。
     * @param errorClass 出错时的分类（DECODE 跳过 PREP_BACKUP 缓冲，直接切）。
     */
    fun action(
        elapsedMs: Long,
        hasBackup: Boolean = true,
        errorClass: PlaybackTelemetry.ErrorClass = PlaybackTelemetry.ErrorClass.NONE,
        onMedia3: Boolean = true,
        vlcAlreadyTried: Boolean = false,
    ): Action = when {
        elapsedMs < 0L -> Action.WAIT
        // 解码类直接跳到切，不再浪费一轮「预备备用线」——同编码内容换源无用。
        errorClass == PlaybackTelemetry.ErrorClass.DECODE && elapsedMs >= NORMAL_MS -> Action.SWITCH
        elapsedMs < NORMAL_MS -> Action.WAIT
        // 3~5s 窗口：没有备用线时「预备备用线」无事可做，但**也还不到换引擎的时候**
        // （换引擎是重建引擎 + 重新起播的重操作，按阶梯应在 5s 之后）。这里保持 SWITCH，
        // 由调用方在 5s 那一档再决定。
        elapsedMs < PREP_BACKUP_MS -> if (hasBackup) Action.PREP_BACKUP else Action.SWITCH
        elapsedMs < SWITCH_MS -> Action.SWITCH
        elapsedMs < HARD_TIMEOUT_MS -> switchOrEngine(hasBackup, onMedia3, vlcAlreadyTried)
        else -> Action.HARD_TIMEOUT
    }

    /**
     * 到点要恢复时：先切线，切不了（没备用线）才换引擎。
     *
     * 换引擎是有代价的操作（重建引擎 + 重新起播），所以只在**确实切不到线**时用；
     * 已经在 libVLC 上则不重复要求换引擎（否则会指向一个已经是当前的引擎）。
     */
    private fun switchOrEngine(
        hasBackup: Boolean,
        onMedia3: Boolean,
        vlcAlreadyTried: Boolean,
    ): Action = when {
        hasBackup -> Action.SWITCH
        onMedia3 && !vlcAlreadyTried -> Action.ENGINE_FALLBACK
        else -> Action.SWITCH
    }

    /**
     * 给「已经硬超时」一个最终判定：到点且仍无首帧 → 真死。
     * 供看门狗在 [Action.HARD_TIMEOUT] 后决定是否触发失败链。
     */
    fun isHardTimeout(elapsedMs: Long): Boolean = elapsedMs >= HARD_TIMEOUT_MS

    /**
     * 快速起播判定：若已有可用线路引用（resolver preferred / last_good / 已解析首选），
     * 立即 prepare，不等全部线路到齐。
     *
     * 这是「点播放秒进」的关键——旧版要等所有线路 match 完才允许 prepare；
     * 现在只要手里有一条能用的线路就先播，剩余线路在后台补齐供用户换线。
     */
    fun shouldStartImmediately(hasPreferredOrLastGood: Boolean, linesReady: Int): Boolean =
        hasPreferredOrLastGood || linesReady > 0

    /**
     * 首帧硬超时（≥15s 仍无画面）时该做什么。
     *
     * 这里**不能**直接复用 [PlayerViewModel.onLineFail] 那条线失败链：那条链的第一步是
     * 「中继失败 → 同线直连重试一次」，而首帧硬超时的含义恰恰是「这条线 15 秒一个画面都
     * 没出」——地址可能根本没取到（同线重解析仍失败），或者取到了但管线出不来帧
     * （换协议再取一次同样解不出来）。对同一处故障重试并重启整个 15s 阶梯，等于让用户
     * 白等第二轮 15s，而真正有用的「换线 / 换引擎」被排在它后面甚至轮不到。
     *
     * 处置顺序（从便宜到昂贵）：
     *  1. [DEGRADE_ENGINE] 有备用线时换线（换源通常比换引擎便宜、成功率更高）；
     *  2. [ENGINE_FALLBACK] 已在 media3 且没试过 libVLC 时换引擎（软解兜底最稳）；
     *  3. [GIVE_UP] 都没有 → 才判死走错误态，让用户看到可操作的提示而不是继续黑屏。
     */
    enum class HardTimeoutAction {
        SWITCH_LINE,
        ENGINE_FALLBACK,
        GIVE_UP,
    }

    fun hardTimeoutAction(
        hasBackupLine: Boolean,
        onMedia3: Boolean,
        vlcAlreadyTried: Boolean,
    ): HardTimeoutAction = when {
        hasBackupLine -> HardTimeoutAction.SWITCH_LINE
        onMedia3 && !vlcAlreadyTried -> HardTimeoutAction.ENGINE_FALLBACK
        else -> HardTimeoutAction.GIVE_UP
    }
}

package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.analytics.PlaybackTelemetry

/**
 * 长时播放健康策略（2026-09-28）。
 *
 * 与 [FirstFrameRecoveryPolicy]（起播阶段）互补，本策略只管「已经出过画面」的播放中段。
 *
 * 核心难点：**BUFFERING 不等于卡死**。播放器状态是 BUFFERING 可能只是正常地补一个分片
 * （网络抖动、码率波动），此时 buffer 还是增长的，位置也会动。旧版卡顿监视
 * （[com.qiubo.optimaltv.playback.PlaybackRecoveryPolicy]）只认「BUFFERING 超过 8s」，
 * 会把这种短暂缓冲误判成卡死并触发换线——这就是「看十几分钟频繁莫名换线」的成因之一。
 *
 * 所以本策略的 BUFFERING 判定**必须结合 buffer 是否在增长**：
 *  - buffer 在涨 / 位置在动 → HEALTHY，只是正常缓冲，什么都不做；
 *  - buffer 不涨或位置不动，且持续够久 → 才判定真卡死，按阶梯处置。
 *
 * 处置阶梯（缓冲中累计卡顿时长）：
 *  - 0–3.5s    HEALTHY        正常缓冲，不干预；
 *  - 3.5–6s    PREP_BACKUP    后台预备备用线（还不打断）；
 *  - 6–12s     SWITCH         切备用线；
 *  - 12–20s    DEGRADE_ENGINE 引擎降级（换 VLC，弱网+硬解组合常挂死）；
 *  - >=20s     GIVE_UP        长时间卡死，触发失败链。
 *
 * **20–30s 切线冷却**：一次恢复动作后，同一条线在冷却窗口内不再被自动切换选中——
 * 刚切过去的线很可能也是慢的（同一区域 CDN、同一运营商），无冷却会「线1→线2→线1」
 * 来回抖，把 3 条线 30s 内耗光，比卡住更糟。
 */
object LongPlaybackHealthPolicy {

    const val PREP_BACKUP_MS = 3_500L
    const val SWITCH_MS = 6_000L
    const val DEGRADE_ENGINE_MS = 12_000L
    const val HARD_TIMEOUT_MS = 20_000L

    /** 切线冷却下限/上限：20–30s。 */
    const val SWITCH_COOLDOWN_MIN_MS = 20_000L
    const val SWITCH_COOLDOWN_MAX_MS = 30_000L

    /** 播放中段的恢复档位。 */
    enum class Action {
        /** 缓冲在涨/位置在动：正常，不干预。 */
        HEALTHY,

        /** 后台预备备用线。 */
        PREP_BACKUP,

        /** 切备用线。 */
        SWITCH,

        /** 引擎降级（换 VLC）。 */
        DEGRADE_ENGINE,

        /** 长时间卡死：放弃当前线，触发失败链。 */
        GIVE_UP,
    }

    /**
     * 核心判定：给定缓冲/位置采样，判断是否真卡死以及该做什么。
     *
     * @param bufferingMs 本次进入 BUFFERING 以来已持续毫秒数。
     * @param bufferGrowing 缓冲区是否还在增长（true = 正常补片，不是卡死）。
     * @param positionMoving 播放位置是否还在前进。
     * @param hasBackup 是否有可用备用线。
     * @param errorClass 错误分类；DECODE 类直接降级引擎（换源无效）。
     */
    fun action(
        bufferingMs: Long,
        bufferGrowing: Boolean,
        positionMoving: Boolean,
        hasBackup: Boolean = true,
        errorClass: PlaybackTelemetry.ErrorClass = PlaybackTelemetry.ErrorClass.NONE,
    ): Action {
        // 解码类优先于「buffer 在涨」的早退判断。
        //
        // 渲染管线死了的时候，buffer 完全可能是满的、位置也照走（时钟在跑、画面冻住），
        // 这正是 isFrozen 描述的形态。旧实现把 `bufferGrowing || positionMoving` 直接
        // 判成 HEALTHY，于是这类冻结会被这道早退吃掉，一路拖到通用恢复阶梯的
        // RELOAD_CURRENT——对解码器死掉毫无意义，只会让用户多等一轮。
        // DECODE 分类是调用方已经观测到渲染无输出时的判定，应当被尊重。
        if (errorClass == PlaybackTelemetry.ErrorClass.DECODE) {
            return if (bufferingMs >= DEGRADE_ENGINE_MS) Action.GIVE_UP else Action.DEGRADE_ENGINE
        }
        // 缓冲在涨或位置在动 → 正常补片，管道没堵死。
        if (bufferGrowing || positionMoving) return Action.HEALTHY
        return when {
            bufferingMs < 0L -> Action.HEALTHY
            bufferingMs < PREP_BACKUP_MS -> Action.HEALTHY
            bufferingMs < SWITCH_MS -> if (hasBackup) Action.PREP_BACKUP else Action.DEGRADE_ENGINE
            bufferingMs < DEGRADE_ENGINE_MS -> Action.SWITCH
            bufferingMs < HARD_TIMEOUT_MS -> Action.DEGRADE_ENGINE
            else -> Action.GIVE_UP
        }
    }

    /**
     * 切线冷却判定：刚在 [lastSwitchAtMs] 切过线，现在又故障，是否允许再切。
     *
     * 冷却窗口 [SWITCH_COOLDOWN_MIN_MS]–[SWITCH_COOLDOWN_MAX_MS]：窗口内不再自动切到
     * 同一条刚失败过的线（允许在冷却后重新尝试，因为瞬时抖动会自愈）。
     *
     * @param nowMs 当前时刻。
     * @param lastSwitchAtMs 上次切线时刻；-1 = 从未切过。
     * @param candidateIndex 本次想切到的线路索引。
     * @param failedIndex 刚失败的那条线索引。
     */
    fun canSwitchNow(
        nowMs: Long,
        lastSwitchAtMs: Long,
        candidateIndex: Int,
        failedIndex: Int,
    ): Boolean {
        if (lastSwitchAtMs < 0L) return true
        val since = nowMs - lastSwitchAtMs
        // 冷却未过，且候选就是刚失败的那条线：拒绝（防抖动）。
        return !(candidateIndex == failedIndex && since < SWITCH_COOLDOWN_MIN_MS)
    }

    /** 冷却是否已过（用于给「刚切的线也不可立刻回切」）。 */
    fun cooldownElapsed(nowMs: Long, lastSwitchAtMs: Long): Boolean =
        lastSwitchAtMs < 0L || (nowMs - lastSwitchAtMs) >= SWITCH_COOLDOWN_MIN_MS

    /**
     * 冻结判定：位置在前进、但渲染帧数长时间不增 = 解码/渲染管线不再出帧。
     *
     * 既有看门狗只认「BUFFERING 超时」与「位置不前进」，对「时钟在走、画面冻住」不响，
     * 用户只能退出重进。这里用渲染帧数做硬信号：位置前进 + 帧数不增持续
     * [FREEZE_POSITION_MOVING_MS] → 冻结，需要重建引擎。
     */
    const val FREEZE_POSITION_MOVING_MS = 30_000L

    fun isFrozen(positionMoving: Boolean, renderedNotIncreasingForMs: Long): Boolean =
        positionMoving && renderedNotIncreasingForMs >= FREEZE_POSITION_MOVING_MS
}

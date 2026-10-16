package com.qiubo.optimaltv.ui.player

/**
 * 点播首帧等待策略。
 *
 * 旧的首帧看门狗只有一个 15s 硬超时，命中就直接换线/换引擎；现在等待期间保留用户
 * 选择的线路，超时延长到 5 分钟。
 * 保留当前影视源，不在等待过程中自动换线；Media3 可以在同一线路上回退到 VLC，
 * 最终起播超时为 5 分钟。
 *
 * 3 秒后记录等待状态，7 秒时若仍在 Media3 上只回退播放引擎；其它时间继续等同一线路，
 * 300 秒时才将起播超时交给失败处理。
 */
object FirstFrameRecoveryPolicy {

    const val NORMAL_MS = 3_000L
    const val PREP_BACKUP_MS = 5_000L
    const val SWITCH_MS = 7_000L
    const val HARD_TIMEOUT_MS = 300_000L
    const val FOOTBALL_HARD_TIMEOUT_MS = 15_000L

    /** 首帧等待的处置档位。 */
    enum class Action {
        /** 继续等，不做任何切换。 */
        WAIT,

        /** 进入慢速等待阶段，不打断当前播放。 */
        PREP_BACKUP,

        /** 仅供足球回放沿用既有自动切线行为。 */
        SWITCH,

        /** 只换播放引擎，保留当前线路。 */
        ENGINE_FALLBACK,

        /** 硬超时：判定当前线路起播失败，等待用户操作。 */
        HARD_TIMEOUT,

        /** 已经出过帧，后续阶段不再需要本阶梯。 */
        DONE,
    }

    /**
     * 按「已等待毫秒数」决定动作。
     *
     * @param elapsedMs 从 prepare 到现在的毫秒数。
     * @param hasBackup 是否存在其它线路；只影响是否记录慢速等待阶段，不自动切换线路。
     */
    fun action(
        elapsedMs: Long,
        hasBackup: Boolean = true,
        onMedia3: Boolean = true,
        vlcAlreadyTried: Boolean = false,
    ): Action = when {
        elapsedMs < 0L -> Action.WAIT
        elapsedMs < NORMAL_MS -> Action.WAIT
        elapsedMs < PREP_BACKUP_MS -> if (hasBackup) Action.PREP_BACKUP else Action.WAIT
        elapsedMs < SWITCH_MS -> Action.PREP_BACKUP
        elapsedMs < HARD_TIMEOUT_MS ->
            if (onMedia3 && !vlcAlreadyTried) Action.ENGINE_FALLBACK else Action.WAIT
        else -> Action.HARD_TIMEOUT
    }

    /** Preserve the existing short recovery ladder for football replays. */
    fun footballAction(
        elapsedMs: Long,
        hasBackup: Boolean,
        onMedia3: Boolean,
        vlcAlreadyTried: Boolean,
    ): Action = when {
        elapsedMs < 0L -> Action.WAIT
        elapsedMs < NORMAL_MS -> Action.WAIT
        elapsedMs < PREP_BACKUP_MS -> if (hasBackup) Action.PREP_BACKUP else Action.SWITCH
        elapsedMs < SWITCH_MS -> Action.SWITCH
        elapsedMs < FOOTBALL_HARD_TIMEOUT_MS ->
            if (hasBackup) Action.SWITCH else if (onMedia3 && !vlcAlreadyTried) Action.ENGINE_FALLBACK else Action.SWITCH
        else -> Action.HARD_TIMEOUT
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
     * 首帧硬超时后只允许最后尝试同一线路的引擎回退；不更改用户选中的影视源。
     */
    enum class HardTimeoutAction {
        ENGINE_FALLBACK,
        GIVE_UP,
    }

    fun hardTimeoutAction(
        onMedia3: Boolean,
        vlcAlreadyTried: Boolean,
    ): HardTimeoutAction = when {
        onMedia3 && !vlcAlreadyTried -> HardTimeoutAction.ENGINE_FALLBACK
        else -> HardTimeoutAction.GIVE_UP
    }

    enum class FootballHardTimeoutAction {
        SWITCH_LINE,
        ENGINE_FALLBACK,
        GIVE_UP,
    }

    fun footballHardTimeoutAction(
        hasBackupLine: Boolean,
        onMedia3: Boolean,
        vlcAlreadyTried: Boolean,
    ): FootballHardTimeoutAction = when {
        hasBackupLine -> FootballHardTimeoutAction.SWITCH_LINE
        onMedia3 && !vlcAlreadyTried -> FootballHardTimeoutAction.ENGINE_FALLBACK
        else -> FootballHardTimeoutAction.GIVE_UP
    }
}

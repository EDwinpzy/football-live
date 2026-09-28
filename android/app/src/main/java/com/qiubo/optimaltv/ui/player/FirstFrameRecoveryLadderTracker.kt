package com.qiubo.optimaltv.ui.player

/**
 * 首帧恢复阶梯的「代次」跟踪器（2026-09-28）。
 *
 * 修的是这样一个真实故障：`armVodFirstFrameLadder` 用 `viewModelScope.launch` 起协程却
 * **没有持有 Job**，于是换线时旧阶梯协程不会被取消，仍然按自己的计时继续跑。它会在
 * 新线路刚起播的 500ms 采样里判定「已等 7s」→ 立刻把新线路切走，用户看到的就是
 * 「刚切成功就被切走、反复横跳」。而新线路自己又起了一条阶梯，多个阶梯互相踩。
 *
 * 协程取消能解决「协程还活着」，但解决不了一个更隐蔽的窗口：旧协程已经通过
 * `isActive` 检查、正在执行动作体，此刻才被取消。**代次校验**补上这个窗口——
 * 每次起播领一个单调递增的代号，所有动作在真正生效前先核对代号是否仍是当前代。
 * 旧代号一律丢弃。
 *
 * 状态刻意保持最小：只需「当前代号 / 本代是否出过帧 / 本代已达最高档」三项。
 * 全部在主线程读写（调用方是 [androidx.lifecycle.ViewModel] 的 viewModelScope），
 * 不需要额外加锁。
 */
class FirstFrameRecoveryLadderTracker {

    private var currentGeneration = 0
    private var sawFirstFrame = false
    private var highestAction: FirstFrameRecoveryPolicy.Action = FirstFrameRecoveryPolicy.Action.WAIT

    /** 新一次起播：作废旧代、武装新代，返回本代代号。 */
    fun beginGeneration(): Int {
        currentGeneration++
        sawFirstFrame = false
        highestAction = FirstFrameRecoveryPolicy.Action.WAIT
        return currentGeneration
    }

    /** [generation] 是否仍是当前代。false = 这是已被换线作废的旧阶梯。 */
    fun isCurrent(generation: Int): Boolean = generation == currentGeneration

    /** 本代是否还允许继续升档（已出帧就不再需要首帧阶梯了）。 */
    fun shouldEscalate(generation: Int): Boolean =
        isCurrent(generation) && !sawFirstFrame

    /** 本代已出首帧：阶梯就此收口。 */
    fun markFirstFrame(generation: Int) {
        if (isCurrent(generation)) sawFirstFrame = true
    }

    /**
     * 只在档位**严格变严重**时放行。
     *
     * 返回 true 表示调用方应当执行该动作。同档位重复触发返回 false——否则同一个
     * 缓冲周期里 500ms 一次的采样会反复执行同一个动作（例如反复切同一条线）。
     */
    fun acceptEscalation(
        generation: Int,
        action: FirstFrameRecoveryPolicy.Action,
    ): Boolean {
        if (!shouldEscalate(generation)) return false
        if (action.ordinal <= highestAction.ordinal) return false
        highestAction = action
        return true
    }
}

package com.qiubo.optimaltv.ui.player

/**
 * 播放中（已出画面）的健康阶梯（方案 §长播健康）。
 *
 * 与 [FirstFrameRecoveryPolicy] 分工：那里管起播，这里管「播着播着卡了」。
 * 阈值比首帧宽松——播到一半的卡顿，短暂抖动不该触发换线。
 *
 * 关键点：**BUFFERING 必须结合 buffer 是否在增长**。
 * ExoPlayer 正常预缓冲也会短暂进 BUFFERING，buffer 在涨就是健康的；
 * 只有 buffer 停住（甚至回落）才算真卡顿。旧逻辑只看状态，正常的起播缓冲
 * 会被误判成故障，代价是频繁无谓换源。
 */
object LongPlaybackHealthPolicy {

    const val PREPARE_BACKUP_MS = 3_500L
    const val SWITCH_BACKUP_MS = 6_000L
    const val ENGINE_DEGRADE_MS = 12_000L

    /** 冷却窗口：换源/换引擎后这段时间内不再重复动作，防抖。 */
    const val COOLDOWN_MIN_MS = 20_000L
    const val COOLDOWN_MAX_MS = 30_000L

    /** buffer 必须至少增长这么多才算「还在正常预缓冲」，容忍分片粒度抖动。 */
    const val BUFFER_GROWTH_EPS_MS = 120L

    enum class Action {
        HEALTHY,
        PREPARE_BACKUP,
        SWITCH_BACKUP,
        ENGINE_DEGRADE,
        COOLDOWN,
        NONE,
    }

    /**
     * @param bufferingMs 连续处于 BUFFERING 的时长
     * @param bufferGrowing 本次采样相比上次，缓冲量是否在增长
     * @param inCooldown 是否处于换源后的冷却窗口内
     */
    fun actionFor(
        bufferingMs: Long,
        bufferGrowing: Boolean,
        inCooldown: Boolean = false,
    ): Action {
        // 冷却期只观测不动手，避免「换源 → 仍在起播 → 又判卡顿 → 再换源」的自激循环。
        if (inCooldown) return Action.COOLDOWN
        if (bufferingMs < PREPARE_BACKUP_MS) return Action.HEALTHY
        // 缓冲在涨 = 正常预缓冲，媒体在下载，只是还没追上播放头。
        if (bufferGrowing) return Action.HEALTHY
        if (bufferingMs < SWITCH_BACKUP_MS) return Action.PREPARE_BACKUP
        if (bufferingMs < ENGINE_DEGRADE_MS) return Action.SWITCH_BACKUP
        return Action.ENGINE_DEGRADE
    }

    /** 缓冲是否算「还在增长」。给出采样间隔供单测构造可复现的序列。 */
    fun isGrowing(previousMs: Long, currentMs: Long): Boolean =
        currentMs - previousMs >= BUFFER_GROWTH_EPS_MS

    /** 冷却时长按故障严重度递增，但封顶 [COOLDOWN_MAX_MS]，免得久到用户以为卡死。 */
    fun cooldownMs(escalationStep: Int): Long {
        val step = escalationStep.coerceAtLeast(0)
        val value = COOLDOWN_MIN_MS + step * 5_000L
        return value.coerceAtMost(COOLDOWN_MAX_MS)
    }

    /**
     * 冷却窗口状态机（换源/换引擎防抖）。
     *
     * 抽出来是因为冷却有两条互相打架的生命周期：换源时**装弹**，
     * 而新线路起播成功（onSessionStarted）会重置会话级计数——旧版在那里顺手把
     * 冷却也清零，等于「刚换的线在起播、正在缓冲」这段最该被保护的窗口立刻失效，
     * 于是健康采样又判卡顿、再换线，形成自激循环。
     *
     * 现在两者分开：**冷却只由故障处置与到期推进**，起播不再清它；
     * 真正把冷却一起清掉的是「同一条线稳定播放满 60s」（见 [onStablePlayback]），
     * 那才是真正值得重新信任的信号。
     */
    class Cooldown {
        /** 冷却截止时刻（elapsedRealtime 口径）；0 = 未在冷却。 */
        var until: Long = 0L
            private set

        /** 换源/换引擎后装弹：窗口只延长不缩短，避免连续处置把冷却越推越远。 */
        fun arm(now: Long, escalationStep: Int) {
            val candidate = now + cooldownMs(escalationStep)
            if (candidate > until) until = candidate
        }

        fun isActive(now: Long): Boolean = until > 0L && now < until

        /**
         * 稳定播放确认：只有真正「这条线撑住了」才清冷却。
         * 换线后随便一 READY 就清 = 自激循环的根源，所以由调用方在满足
         * 稳定时长后显式调用。
         */
        fun onStablePlayback() {
            until = 0L
        }

        /** 播放彻底结束/换片：会话级状态该清干净。 */
        fun reset() {
            until = 0L
        }
    }

    /**
     * 会话级恢复预算：给「僵尸源」循环封顶。
     *
     * 问题：恢复阶梯本身在 [com.qiubo.optimaltv.playback.PlaybackRecoveryPolicy] 里是收敛的
     * （第 4 步 GIVE_UP），但计数 `recoveryAttempt` 会在**每次起播成功**时清零。
     * 于是一条「能出画面、几秒后必断」的僵尸线可以无限循环：
     * 起播 → 清零 → 卡顿 → 换线 → 起播 → 清零 → …
     * 观众看到的是永远转圈的播放器，日志里 rbuf 计数一路上涨（见 onSessionStarted 注释）。
     *
     * 真正的解药是「稳定播放满 60s 才恢复预算」（既有 sameLineRetries 逻辑），
     * 但那是**事后**恢复；这里再加一道**总量**上限，保证「从未稳定过」的会话
     * 也有硬顶：连续 [MAX_UNSTABLE_RECOVERIES] 次恢复都没有一次稳定播放 → 放弃。
     */
    class RecoveryBudget(private val maxUnstableRecoveries: Int = MAX_UNSTABLE_RECOVERIES) {
        private var unstableRecoveries = 0
        private var stableSinceLastRecovery = false

        /** 已消耗的恢复次数（供日志/埋点）。 */
        val spent: Int get() = unstableRecoveries

        /** 还能再恢复几次。0 = 该收手了。 */
        val remaining: Int get() = (maxUnstableRecoveries - unstableRecoveries).coerceAtLeast(0)

        /**
         * 申请一次恢复许可。返回 false = 已超预算，调用方应走 GIVE_UP。
         *
         * 语义：[stablePlayback] 为 true 时预算恢复（这条线撑住了，值得再给它机会），
         * 否则计一次消耗。
         */
        fun requestRecovery(stablePlayback: Boolean = false): Boolean {
            stableSinceLastRecovery = stablePlayback
            if (stablePlayback) {
                unstableRecoveries = 0
                return true
            }
            if (unstableRecoveries >= maxUnstableRecoveries) return false
            unstableRecoveries++
            return true
        }

        /**
         * 本次起播是否「稳定过」。起播成功时调用：
         * 只有真正撑满稳定时长（60s 体检达标）才传 true，否则起播成功只记 false。
         */
        fun markStablePlayback() {
            stableSinceLastRecovery = true
        }

        /**
         * 这条线真的稳住了（连续 60s 在播）→ 预算归零恢复。
         * 与 [markStablePlayback] 的区别：这里是**事后**的确认（体检达标时调用），
         * 会真的清零消耗；而 [markStablePlayback] 只打标记。
         */
        fun onStablePlayback() {
            unstableRecoveries = 0
            stableSinceLastRecovery = true
        }

        /** 换片/退出：会话结束，预算归零。 */
        fun reset() {
            unstableRecoveries = 0
            stableSinceLastRecovery = false
        }
    }

    /** 一次会话内、从未稳定播放过的最大恢复次数。超过即放弃，避免僵尸源无限循环。 */
    const val MAX_UNSTABLE_RECOVERIES = 6
}

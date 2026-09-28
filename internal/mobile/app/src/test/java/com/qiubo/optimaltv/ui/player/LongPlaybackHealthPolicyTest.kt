package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LongPlaybackHealthPolicyTest {

    private fun action(bufferingMs: Long, growing: Boolean, inCooldown: Boolean = false) =
        LongPlaybackHealthPolicy.actionFor(bufferingMs, growing, inCooldown)

    @Test
    fun shortBufferingIsHealthyRegardlessOfGrowth() {
        assertEquals(LongPlaybackHealthPolicy.Action.HEALTHY, action(0L, growing = false))
        assertEquals(LongPlaybackHealthPolicy.Action.HEALTHY, action(3_499L, growing = false))
    }

    @Test
    fun growingBufferStaysHealthyPastEveryThreshold() {
        // 这是本策略的核心：正常预缓冲会进 BUFFERING，但 buffer 在涨就说明媒体在下载，
        // 只是还没追上播放头。旧口径只看状态，会在这里白白换源。
        assertEquals(LongPlaybackHealthPolicy.Action.HEALTHY, action(4_000L, growing = true))
        assertEquals(LongPlaybackHealthPolicy.Action.HEALTHY, action(7_000L, growing = true))
        assertEquals(LongPlaybackHealthPolicy.Action.HEALTHY, action(30_000L, growing = true))
    }

    @Test
    fun stalledBufferEscalatesThroughTheLadder() {
        assertEquals(LongPlaybackHealthPolicy.Action.PREPARE_BACKUP, action(3_500L, growing = false))
        assertEquals(LongPlaybackHealthPolicy.Action.SWITCH_BACKUP, action(6_000L, growing = false))
        assertEquals(LongPlaybackHealthPolicy.Action.ENGINE_DEGRADE, action(12_000L, growing = false))
        assertEquals(LongPlaybackHealthPolicy.Action.ENGINE_DEGRADE, action(60_000L, growing = false))
    }

    @Test
    fun cooldownSuppressesEveryAction() {
        // 换源后正在起播，此时 BUFFERING 属正常；冷却期内不得再触发，避免自激换源。
        assertEquals(LongPlaybackHealthPolicy.Action.COOLDOWN, action(500L, growing = false, inCooldown = true))
        assertEquals(LongPlaybackHealthPolicy.Action.COOLDOWN, action(20_000L, growing = false, inCooldown = true))
        // 冷却期内连「buffer 停住 12s」也不动手
        assertEquals(
            LongPlaybackHealthPolicy.Action.COOLDOWN,
            action(12_000L, growing = false, inCooldown = true),
        )
    }

    @Test
    fun growthNeedsToClearTheEpsilon() {
        // 分片粒度抖动不该算「在增长」，否则正常的锯齿会被判成健康。
        assertFalse(LongPlaybackHealthPolicy.isGrowing(1_000L, 1_050L))
        assertFalse(LongPlaybackHealthPolicy.isGrowing(1_000L, 900L))
        assertTrue(LongPlaybackHealthPolicy.isGrowing(1_000L, 1_120L))
        assertTrue(LongPlaybackHealthPolicy.isGrowing(1_000L, 5_000L))
    }

    @Test
    fun cooldownGrowsWithEscalationButIsCapped() {
        assertEquals(20_000L, LongPlaybackHealthPolicy.cooldownMs(0))
        assertEquals(25_000L, LongPlaybackHealthPolicy.cooldownMs(1))
        // 封顶 30s：再久用户会以为应用死了
        assertEquals(30_000L, LongPlaybackHealthPolicy.cooldownMs(2))
        assertEquals(30_000L, LongPlaybackHealthPolicy.cooldownMs(99))
        // 负数档位不产生负冷却
        assertEquals(20_000L, LongPlaybackHealthPolicy.cooldownMs(-5))
    }

    /* ---------- 冷却状态机：修复「换线后冷却被起播清零」的自激循环 ---------- */

    @Test
    fun cooldownSurvivesTheNewLineComingUp() {
        // 场景：t=10s 判卡顿换线 → 装弹到 t=30s（窗口 20s）；新线在 t=10.03s 就 READY。
        // 旧版 onSessionStarted 无条件 healthCooldownUntil = 0，冷却当场作废，
        // 于是「刚换的线正在起播、缓冲属正常」这段最该被保护的窗口毫无保护，
        // 健康采样很快又判卡顿 → 再换线，来回打摆。
        val cd = LongPlaybackHealthPolicy.Cooldown()
        cd.arm(now = 10_000L, escalationStep = 0)
        assertEquals(30_000L, cd.until)
        assertTrue(cd.isActive(10_000L))

        // 新线起播（t=10.03s）：起播本身**不得**清冷却
        assertTrue("新线起播后冷却仍在", cd.isActive(10_003L))
        assertTrue(cd.isActive(29_999L))
        // 到期后自然失效
        assertFalse(cd.isActive(30_000L))
    }

    @Test
    fun cooldownIsClearedOnlyByStablePlaybackOrSessionEnd() {
        val cd = LongPlaybackHealthPolicy.Cooldown()
        cd.arm(0L, 0)
        assertTrue(cd.isActive(1_000L))
        // 稳定播放确认（60s 体检达标）才清
        cd.onStablePlayback()
        assertFalse(cd.isActive(1_000L))
        // 换片/退出：会话级状态清干净
        cd.arm(0L, 0)
        assertTrue(cd.isActive(1L))
        cd.reset()
        assertFalse(cd.isActive(1L))
    }

    @Test
    fun repeatedArmingNeverShortensAnActiveWindow() {
        val cd = LongPlaybackHealthPolicy.Cooldown()
        cd.arm(now = 0L, escalationStep = 0)      // → 20s
        cd.arm(now = 1_000L, escalationStep = 0)  // → 21s（延长）
        val first = cd.until
        assertEquals(21_000L, first)
        // 重复装弹只延长不缩短：再晚 1s 装弹，窗口推到 22s，而不是把 21s 拉回来
        cd.arm(now = 2_000L, escalationStep = 0)
        assertEquals(22_000L, cd.until)
        assertTrue(cd.until >= first)
        // 一次「更轻」的装弹不能把窗口拉短，否则会提前解除保护
        cd.arm(now = 500L, escalationStep = 0)
        assertEquals(22_000L, cd.until)
    }

    @Test
    fun cooldownIsInactiveBeforeAnythingArmsIt() {
        val cd = LongPlaybackHealthPolicy.Cooldown()
        assertFalse(cd.isActive(0L))
        assertFalse(cd.isActive(Long.MAX_VALUE / 2))
    }

    /* ---------- 恢复预算：给「僵尸源」无限循环封顶 ---------- */

    @Test
    fun unstableRecoveryLoopIsCapped() {
        // 僵尸线：起播→清零→卡顿→换线→起播→清零…，每次都没撑满 60s。
        // 旧版 recoveryAttempt 每次起播成功都清零，永远到不了 PlaybackRecoveryPolicy 的 GIVE_UP，
        // 播放器无限转圈。预算耗尽必须拒绝下一次恢复。
        val budget = LongPlaybackHealthPolicy.RecoveryBudget(maxUnstableRecoveries = 3)
        assertTrue(budget.requestRecovery(stablePlayback = false))
        assertTrue(budget.requestRecovery(stablePlayback = false))
        assertTrue(budget.requestRecovery(stablePlayback = false))
        // 第四次：超预算 → 调用方必须走 GIVE_UP
        assertFalse(budget.requestRecovery(stablePlayback = false))
        assertEquals(0, budget.remaining)
        // 已耗尽后继续拒绝（不因再申请一次就放行）
        assertFalse(budget.requestRecovery(stablePlayback = false))
    }

    @Test
    fun stablePlaybackRestoresTheRecoveryBudget() {
        val budget = LongPlaybackHealthPolicy.RecoveryBudget(maxUnstableRecoveries = 3)
        repeat(3) { budget.requestRecovery(stablePlayback = false) }
        assertEquals(0, budget.remaining)
        // 这条线真撑住了 → 预算恢复，值得再给它机会
        assertTrue(budget.requestRecovery(stablePlayback = true))
        assertEquals(3, budget.remaining)
    }

    @Test
    fun recoveryBudgetIsScopedToOneSession() {
        val budget = LongPlaybackHealthPolicy.RecoveryBudget(maxUnstableRecoveries = 2)
        budget.requestRecovery(stablePlayback = false)
        budget.requestRecovery(stablePlayback = false)
        assertFalse(budget.requestRecovery(stablePlayback = false))
        // 换片/退出 → 新会话从零开始，不能让上一集的烂线拖垮下一集
        budget.reset()
        assertEquals(2, budget.remaining)
        assertTrue(budget.requestRecovery(stablePlayback = false))
    }

    @Test
    fun defaultBudgetIsBounded() {
        // 默认预算必须是有限值——无上限的「恢复」等于没有恢复。
        val budget = LongPlaybackHealthPolicy.RecoveryBudget()
        assertTrue(LongPlaybackHealthPolicy.MAX_UNSTABLE_RECOVERIES in 1..20)
        var allowed = 0
        while (budget.requestRecovery(stablePlayback = false)) allowed++
        assertEquals(LongPlaybackHealthPolicy.MAX_UNSTABLE_RECOVERIES, allowed)
    }
}

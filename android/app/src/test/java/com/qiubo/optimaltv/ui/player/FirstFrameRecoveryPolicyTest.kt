package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.analytics.PlaybackTelemetry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [FirstFrameRecoveryPolicy] 分级阶梯单测。
 *
 * 这些断言刻意踩在**每一条档位边界的前一毫秒 / 后一毫秒**上，且**用字面量而不是
 * 常量本身**钉住边界——只测中间值、或拿被测常量当期望值的话，改了常量测试照样绿
 * （self-referential assertion），起播黑屏照样复现。下面的 LOCAL 段就是为此存在：
 * 有人把 NORMAL_MS 从 3s 改成 3.5s，LITERAL 段的断言会立刻红。
 */
class FirstFrameRecoveryPolicyTest {

    /** 与实现无关的期望值。改实现常量 → 这里红。 */
    private object Spec {
        const val NORMAL_MS = 3_000L
        const val PREP_BACKUP_MS = 5_000L
        const val SWITCH_MS = 7_000L
        const val HARD_TIMEOUT_MS = 15_000L
    }

    @Test fun `the shipped thresholds match the spec`() {
        assertEquals(Spec.NORMAL_MS, FirstFrameRecoveryPolicy.NORMAL_MS)
        assertEquals(Spec.PREP_BACKUP_MS, FirstFrameRecoveryPolicy.PREP_BACKUP_MS)
        assertEquals(Spec.SWITCH_MS, FirstFrameRecoveryPolicy.SWITCH_MS)
        assertEquals(Spec.HARD_TIMEOUT_MS, FirstFrameRecoveryPolicy.HARD_TIMEOUT_MS)
    }

    @Test fun `boundary at 3s turns WAIT into PREP_BACKUP`() {
        assertEquals(
            FirstFrameRecoveryPolicy.Action.WAIT,
            FirstFrameRecoveryPolicy.action(Spec.NORMAL_MS - 1),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Action.PREP_BACKUP,
            FirstFrameRecoveryPolicy.action(Spec.NORMAL_MS),
        )
    }

    @Test fun `boundary at 5s turns PREP_BACKUP into SWITCH`() {
        assertEquals(
            FirstFrameRecoveryPolicy.Action.PREP_BACKUP,
            FirstFrameRecoveryPolicy.action(Spec.PREP_BACKUP_MS - 1),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Action.SWITCH,
            FirstFrameRecoveryPolicy.action(Spec.PREP_BACKUP_MS),
        )
    }

    @Test fun `boundary at 15s turns SWITCH into HARD_TIMEOUT`() {
        assertEquals(
            FirstFrameRecoveryPolicy.Action.SWITCH,
            FirstFrameRecoveryPolicy.action(Spec.HARD_TIMEOUT_MS - 1),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Action.HARD_TIMEOUT,
            FirstFrameRecoveryPolicy.action(Spec.HARD_TIMEOUT_MS),
        )
    }

    @Test fun `the whole ladder never goes backwards as time passes`() {
        // 阶梯必须单调升档，否则看门狗会在 PREP_BACKUP / SWITCH 之间来回抖。
        val ranks = (0L..20_000L step 250L).map { elapsed ->
            when (FirstFrameRecoveryPolicy.action(elapsed, hasBackup = true)) {
                FirstFrameRecoveryPolicy.Action.WAIT -> 0
                FirstFrameRecoveryPolicy.Action.PREP_BACKUP -> 1
                FirstFrameRecoveryPolicy.Action.SWITCH -> 2
                FirstFrameRecoveryPolicy.Action.ENGINE_FALLBACK -> 3
                FirstFrameRecoveryPolicy.Action.HARD_TIMEOUT -> 4
                FirstFrameRecoveryPolicy.Action.DONE -> 5
            }
        }
        assertEquals(ranks.sorted(), ranks)
    }

    @Test fun `without a backup line PREP_BACKUP is pointless and escalates straight to SWITCH`() {
        // 没有备用线时「预备备用线」无事可做，必须直接切，否则会在 3~5s 空窗里什么都不做。
        assertEquals(
            FirstFrameRecoveryPolicy.Action.SWITCH,
            FirstFrameRecoveryPolicy.action(
                elapsedMs = Spec.NORMAL_MS,
                hasBackup = false,
            ),
        )
    }

    @Test fun `decode failure skips the prep-backup wait and switches immediately`() {
        // 解码类故障换线路大概率还是同样编码内容，白等一轮是纯浪费。
        assertEquals(
            FirstFrameRecoveryPolicy.Action.WAIT,
            FirstFrameRecoveryPolicy.action(
                elapsedMs = Spec.NORMAL_MS - 1,
                errorClass = PlaybackTelemetry.ErrorClass.DECODE,
            ),
        )
        assertEquals(
            FirstFrameRecoveryPolicy.Action.SWITCH,
            FirstFrameRecoveryPolicy.action(
                elapsedMs = Spec.NORMAL_MS,
                errorClass = PlaybackTelemetry.ErrorClass.DECODE,
            ),
        )
    }

    @Test fun `network failure still gets the benefit of the doubt`() {
        // 网络类（超时/连不上）先给 PREP_BACKUP 一次机会——瞬时抖动很常见。
        assertEquals(
            FirstFrameRecoveryPolicy.Action.PREP_BACKUP,
            FirstFrameRecoveryPolicy.action(
                elapsedMs = Spec.NORMAL_MS + 100,
                errorClass = PlaybackTelemetry.ErrorClass.NETWORK,
            ),
        )
    }

    @Test fun `negative elapsed never triggers a switch`() {
        // 时钟回拨/未初始化时不能误判成故障。
        assertEquals(FirstFrameRecoveryPolicy.Action.WAIT, FirstFrameRecoveryPolicy.action(-1L))
        assertEquals(FirstFrameRecoveryPolicy.Action.WAIT, FirstFrameRecoveryPolicy.action(-10_000L))
    }

    @Test fun `isHardTimeout agrees with the ladder's hard timeout boundary`() {
        assertFalse(FirstFrameRecoveryPolicy.isHardTimeout(Spec.HARD_TIMEOUT_MS - 1))
        assertTrue(FirstFrameRecoveryPolicy.isHardTimeout(Spec.HARD_TIMEOUT_MS))
        assertTrue(FirstFrameRecoveryPolicy.isHardTimeout(Spec.HARD_TIMEOUT_MS + 60_000))
    }

    @Test fun `a known preferred or last-good line starts playback without waiting for all lines`() {
        // 快速起播：只要手里有一条能用线路就 prepare，不等其余线路匹配完。
        assertTrue(
            FirstFrameRecoveryPolicy.shouldStartImmediately(
                hasPreferredOrLastGood = true,
                linesReady = 0,
            ),
        )
        assertTrue(
            FirstFrameRecoveryPolicy.shouldStartImmediately(
                hasPreferredOrLastGood = false,
                linesReady = 1,
            ),
        )
        assertFalse(
            FirstFrameRecoveryPolicy.shouldStartImmediately(
                hasPreferredOrLastGood = false,
                linesReady = 0,
            ),
        )
    }

    @Test fun `a decode failure escalates to switch before the normal window`() {
        // 解码挂死不该等满 NORMAL_MS：同编码内容多等一秒都是白等。
        assertEquals(
            FirstFrameRecoveryPolicy.Action.SWITCH,
            FirstFrameRecoveryPolicy.action(
                elapsedMs = Spec.NORMAL_MS,
                errorClass = PlaybackTelemetry.ErrorClass.DECODE,
            ),
        )
    }

    @Test fun `the engine fallback is reachable even when no other line exists`() {
        // 这是本轮修的一个真实死分支：旧实现在 PlayerViewModel 里把「切引擎」放在
        // `next != null`（还有备用线）的 when 分支里，于是**只剩一条线**时——
        // 恰恰是最该换引擎的时刻——永远落进 `next == null -> Unit` 什么也不做，
        // 一直耗到 15s 硬超时。策略层要把这个决定显式化，避免再被顺序写错。
        assertEquals(
            "只剩一条线且仍在 media3 上时，首帧超时必须直接换引擎",
            FirstFrameRecoveryPolicy.Action.ENGINE_FALLBACK,
            FirstFrameRecoveryPolicy.action(
                elapsedMs = Spec.SWITCH_MS + 1,
                hasBackup = false,
                onMedia3 = true,
                vlcAlreadyTried = false,
            ),
        )
        // 还有备用线时优先切线（换源比换引擎便宜，且同编码内容下更可能成功）。
        assertEquals(
            FirstFrameRecoveryPolicy.Action.SWITCH,
            FirstFrameRecoveryPolicy.action(
                elapsedMs = Spec.SWITCH_MS + 1,
                hasBackup = true,
                onMedia3 = true,
                vlcAlreadyTried = false,
            ),
        )
    }

    @Test fun `with both a spare line and a spent vlc fallback the ladder still switches`() {
        // libVLC 已经试过（media3 仍是当前引擎）时，不能再要求换引擎，
        // 否则会落进既不换线也不换引擎的空档。
        assertEquals(
            FirstFrameRecoveryPolicy.Action.SWITCH,
            FirstFrameRecoveryPolicy.action(
                elapsedMs = Spec.SWITCH_MS + 1,
                hasBackup = true,
                onMedia3 = true,
                vlcAlreadyTried = true,
            ),
        )
        assertEquals(
            "已在 vlc 上则不应再要求切 vlc",
            FirstFrameRecoveryPolicy.Action.SWITCH,
            FirstFrameRecoveryPolicy.action(
                elapsedMs = Spec.SWITCH_MS + 1,
                hasBackup = false,
                onMedia3 = false,
                vlcAlreadyTried = false,
            ),
        )
    }

    @Test fun `a decode failure with no engine left to fall back to still switches`() {
        // 已在 vlc 且没有备用线 → 没有更激进的手段可用，只能走失败链（SWITCH 之后
        // 由硬超时收口）。这里锁住「不返回 ENGINE_FALLBACK」，避免换到不存在的引擎。
        assertFalse(
            FirstFrameRecoveryPolicy.action(
                elapsedMs = Spec.SWITCH_MS + 1,
                hasBackup = false,
                onMedia3 = false,
                vlcAlreadyTried = false,
            ) == FirstFrameRecoveryPolicy.Action.ENGINE_FALLBACK,
        )
    }

    @Test fun `a stale ladder from the previous line cannot switch the new line`() {
        // 换线时旧阶梯协程若没被取消，它会在新线刚起播时按「已等 7s」判定并切走新线——
        // 表现为「刚切成功就被立刻切走」，反复横跳。这条锁住代次校验：
        // 阶梯只对自己发起时那一代线路生效。
        val ladder = FirstFrameRecoveryLadderTracker()
        val gen1 = ladder.beginGeneration()
        assertTrue(ladder.isCurrent(gen1))

        val gen2 = ladder.beginGeneration()   // 换线 → 新一代
        assertFalse("旧代阶梯必须失效，否则会切走新线路", ladder.isCurrent(gen1))
        assertTrue(ladder.isCurrent(gen2))
    }

    @Test fun `a ladder that already saw a frame stops escalating`() {
        val ladder = FirstFrameRecoveryLadderTracker()
        val gen = ladder.beginGeneration()
        assertTrue(ladder.isCurrent(gen))
        // 引擎报出首帧 → 本代阶梯就此收口，之后的 SWITCH/HARD_TIMEOUT 都不该再来。
        ladder.markFirstFrame(gen)
        assertFalse(ladder.shouldEscalate(gen))
        // 换线后新的一代又重新武装。
        val gen2 = ladder.beginGeneration()
        assertTrue(ladder.shouldEscalate(gen2))
    }

    @Test fun `only ever escalates to a strictly more severe action`() {
        val ladder = FirstFrameRecoveryLadderTracker()
        val gen = ladder.beginGeneration()
        // 同档位重复触发不应重复动作（否则同一缓冲周期里反复切线）。
        assertTrue(ladder.acceptEscalation(gen, FirstFrameRecoveryPolicy.Action.PREP_BACKUP))
        assertFalse(ladder.acceptEscalation(gen, FirstFrameRecoveryPolicy.Action.PREP_BACKUP))
        assertTrue(ladder.acceptEscalation(gen, FirstFrameRecoveryPolicy.Action.SWITCH))
        assertFalse(ladder.acceptEscalation(gen, FirstFrameRecoveryPolicy.Action.PREP_BACKUP))
    }

    @Test fun `hard timeout switches a line before touching the engine`() {
        // 有备用线时换线优先：换源比换引擎便宜、成功率更高。
        assertEquals(
            FirstFrameRecoveryPolicy.HardTimeoutAction.SWITCH_LINE,
            FirstFrameRecoveryPolicy.hardTimeoutAction(
                hasBackupLine = true,
                onMedia3 = true,
                vlcAlreadyTried = false,
            ),
        )
    }

    @Test fun `hard timeout falls back to the engine when no line is left`() {
        // 只剩一条线且还在 media3 → 换 libVLC。这正是旧实现永远够不着的场景。
        assertEquals(
            FirstFrameRecoveryPolicy.HardTimeoutAction.ENGINE_FALLBACK,
            FirstFrameRecoveryPolicy.hardTimeoutAction(
                hasBackupLine = false,
                onMedia3 = true,
                vlcAlreadyTried = false,
            ),
        )
    }

    @Test fun `hard timeout only gives up once there is nothing left to try`() {
        // 已在 libVLC 且没有备用线：换线与换引擎都无意义，才判死。
        assertEquals(
            FirstFrameRecoveryPolicy.HardTimeoutAction.GIVE_UP,
            FirstFrameRecoveryPolicy.hardTimeoutAction(
                hasBackupLine = false,
                onMedia3 = false,
                vlcAlreadyTried = false,
            ),
        )
        assertEquals(
            "已在 vlc 且有备用线时仍应先换线",
            FirstFrameRecoveryPolicy.HardTimeoutAction.SWITCH_LINE,
            FirstFrameRecoveryPolicy.hardTimeoutAction(
                hasBackupLine = true,
                onMedia3 = false,
                vlcAlreadyTried = true,
            ),
        )
    }

    @Test fun `the engine fallback waits for the switch window instead of firing at 3s`() {
        // 换引擎 = 释放引擎 + 新建 + 重新起播，是重操作；按阶梯它属于 5s 之后。
        // 只有一条线时 3~5s 仍然返回 SWITCH（由调用方在 5s 档再决定换引擎），
        // 否则会出现「3s 就把引擎换掉」的过早动作。
        assertEquals(
            FirstFrameRecoveryPolicy.Action.SWITCH,
            FirstFrameRecoveryPolicy.action(
                elapsedMs = Spec.NORMAL_MS + 100,
                hasBackup = false,
                onMedia3 = true,
                vlcAlreadyTried = false,
            ),
        )
    }
}

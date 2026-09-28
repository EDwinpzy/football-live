package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.analytics.PlaybackTelemetry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [LongPlaybackHealthPolicy] 单测。
 *
 * 最要紧的一条是 `buffer 在涨就不算卡死`——旧版只看「BUFFERING 满 8s」就把正常补片
 * 判成卡死并换线，这是「看十几分钟莫名换台」的根因。这条断言如果挂了，说明改坏了。
 */
class LongPlaybackHealthPolicyTest {

    /** 与实现无关的期望值。改实现常量 → 这里红。 */
    private object Spec {
        const val PREP_BACKUP_MS = 3_500L
        const val SWITCH_MS = 6_000L
        const val DEGRADE_ENGINE_MS = 12_000L
        const val HARD_TIMEOUT_MS = 20_000L
        const val COOLDOWN_MIN_MS = 20_000L
        const val COOLDOWN_MAX_MS = 30_000L
        const val FREEZE_MS = 30_000L
    }

    @Test fun `the shipped thresholds match the spec`() {
        assertEquals(Spec.PREP_BACKUP_MS, LongPlaybackHealthPolicy.PREP_BACKUP_MS)
        assertEquals(Spec.SWITCH_MS, LongPlaybackHealthPolicy.SWITCH_MS)
        assertEquals(Spec.DEGRADE_ENGINE_MS, LongPlaybackHealthPolicy.DEGRADE_ENGINE_MS)
        assertEquals(Spec.HARD_TIMEOUT_MS, LongPlaybackHealthPolicy.HARD_TIMEOUT_MS)
        assertEquals(Spec.COOLDOWN_MIN_MS, LongPlaybackHealthPolicy.SWITCH_COOLDOWN_MIN_MS)
        assertEquals(Spec.COOLDOWN_MAX_MS, LongPlaybackHealthPolicy.SWITCH_COOLDOWN_MAX_MS)
        assertEquals(Spec.FREEZE_MS, LongPlaybackHealthPolicy.FREEZE_POSITION_MOVING_MS)
    }

    @Test fun `a growing buffer is never treated as a stall no matter how long`() {
        // 缓冲还在涨 = 管道没堵死，哪怕 BUFFERING 状态挂了很久也不该动它。
        listOf(0L, 1_000L, 5_000L, 30_000L, 600_000L).forEach { ms ->
            assertEquals(
                "buffer 仍在增长时 ${ms}ms 不该判卡死",
                LongPlaybackHealthPolicy.Action.HEALTHY,
                LongPlaybackHealthPolicy.action(ms, bufferGrowing = true, positionMoving = false),
            )
        }
    }

    @Test fun `a moving position is never treated as a stall`() {
        listOf(0L, 5_000L, 30_000L).forEach { ms ->
            assertEquals(
                "位置仍在前进时 ${ms}ms 不该判卡死",
                LongPlaybackHealthPolicy.Action.HEALTHY,
                LongPlaybackHealthPolicy.action(ms, bufferGrowing = false, positionMoving = true),
            )
        }
    }

    @Test fun `a dead buffer escalates through the ladder at each boundary`() {
        fun at(ms: Long) = LongPlaybackHealthPolicy.action(
            bufferingMs = ms,
            bufferGrowing = false,
            positionMoving = false,
            hasBackup = true,
        )
        assertEquals(LongPlaybackHealthPolicy.Action.HEALTHY, at(Spec.PREP_BACKUP_MS - 1))
        assertEquals(LongPlaybackHealthPolicy.Action.PREP_BACKUP, at(Spec.PREP_BACKUP_MS))
        assertEquals(LongPlaybackHealthPolicy.Action.PREP_BACKUP, at(Spec.SWITCH_MS - 1))
        assertEquals(LongPlaybackHealthPolicy.Action.SWITCH, at(Spec.SWITCH_MS))
        assertEquals(LongPlaybackHealthPolicy.Action.SWITCH, at(Spec.DEGRADE_ENGINE_MS - 1))
        assertEquals(LongPlaybackHealthPolicy.Action.DEGRADE_ENGINE, at(Spec.DEGRADE_ENGINE_MS))
        assertEquals(
            LongPlaybackHealthPolicy.Action.DEGRADE_ENGINE,
            at(Spec.HARD_TIMEOUT_MS - 1),
        )
        assertEquals(LongPlaybackHealthPolicy.Action.GIVE_UP, at(Spec.HARD_TIMEOUT_MS))
    }

    @Test fun `with no backup line a dead buffer degrades the engine instead of preparing nothing`() {
        // 没有备用线时 PREP_BACKUP 无意义，直接降级引擎。
        assertEquals(
            LongPlaybackHealthPolicy.Action.DEGRADE_ENGINE,
            LongPlaybackHealthPolicy.action(
                bufferingMs = Spec.PREP_BACKUP_MS,
                bufferGrowing = false,
                positionMoving = false,
                hasBackup = false,
            ),
        )
    }

    @Test fun `decode failure degrades the engine without wasting a source switch`() {
        val degradeAt = LongPlaybackHealthPolicy.action(
            bufferingMs = Spec.PREP_BACKUP_MS,
            bufferGrowing = false,
            positionMoving = false,
            errorClass = PlaybackTelemetry.ErrorClass.DECODE,
        )
        // 3.5s 就该降级引擎，而不是先 SWITCH 一次（换源对解码无效）。
        assertEquals(LongPlaybackHealthPolicy.Action.DEGRADE_ENGINE, degradeAt)
        assertEquals(
            LongPlaybackHealthPolicy.Action.GIVE_UP,
            LongPlaybackHealthPolicy.action(
                bufferingMs = Spec.DEGRADE_ENGINE_MS,
                bufferGrowing = false,
                positionMoving = false,
                errorClass = PlaybackTelemetry.ErrorClass.DECODE,
            ),
        )
    }

    @Test fun `switch cooldown blocks an immediate bounce back to the line that just failed`() {
        val lastSwitch = 100_000L
        // 刚切线 5s 后又故障，且候选就是刚失败的那条 → 拒绝（防抖动）。
        assertFalse(
            LongPlaybackHealthPolicy.canSwitchNow(
                nowMs = lastSwitch + 5_000,
                lastSwitchAtMs = lastSwitch,
                candidateIndex = 1,
                failedIndex = 1,
            ),
        )
        // 冷却满 20s 后允许再试那条线（瞬时抖动会自愈）。
        assertTrue(
            LongPlaybackHealthPolicy.canSwitchNow(
                nowMs = lastSwitch + Spec.COOLDOWN_MIN_MS,
                lastSwitchAtMs = lastSwitch,
                candidateIndex = 1,
                failedIndex = 1,
            ),
        )
    }

    @Test fun `cooldown never blocks a switch to a different line`() {
        val lastSwitch = 100_000L
        // 冷却只拦「回切刚失败的那条」，切别的线永远放行——否则冷却会把恢复链锁死。
        assertTrue(
            LongPlaybackHealthPolicy.canSwitchNow(
                nowMs = lastSwitch + 1_000,
                lastSwitchAtMs = lastSwitch,
                candidateIndex = 2,
                failedIndex = 1,
            ),
        )
    }

    @Test fun `cooldown is open before the first switch and closes inside the window`() {
        val lastSwitch = 100_000L
        assertTrue(LongPlaybackHealthPolicy.cooldownElapsed(50_000L, lastSwitchAtMs = -1L))
        assertFalse(LongPlaybackHealthPolicy.cooldownElapsed(lastSwitch + 1_000, lastSwitch))
        assertTrue(
            LongPlaybackHealthPolicy.cooldownElapsed(
                lastSwitch + LongPlaybackHealthPolicy.SWITCH_COOLDOWN_MIN_MS,
                lastSwitch,
            ),
        )
    }

    @Test fun `cooldown window stays inside the documented 20 to 30 second band`() {
        assertEquals(Spec.COOLDOWN_MIN_MS, LongPlaybackHealthPolicy.SWITCH_COOLDOWN_MIN_MS)
        assertEquals(Spec.COOLDOWN_MAX_MS, LongPlaybackHealthPolicy.SWITCH_COOLDOWN_MAX_MS)
        assertTrue(
            LongPlaybackHealthPolicy.SWITCH_COOLDOWN_MIN_MS < LongPlaybackHealthPolicy.SWITCH_COOLDOWN_MAX_MS,
        )
    }

    @Test fun `a frozen picture with a moving clock is caught by the decoder counter not the watchdog`() {
        // 「时钟在走、画面冻住」：位置前进但渲染帧数不增，既有看门狗（只看 BUFFERING 超时与
        // 位置停滞）全都不响，只能靠渲染帧数这个硬信号。
        assertFalse(
            LongPlaybackHealthPolicy.isFrozen(
                positionMoving = true,
                renderedNotIncreasingForMs = Spec.FREEZE_MS - 1,
            ),
        )
        assertTrue(
            LongPlaybackHealthPolicy.isFrozen(
                positionMoving = true,
                renderedNotIncreasingForMs = Spec.FREEZE_MS,
            ),
        )
        // 位置也没动时不算「冻结」——那是纯卡死，走另一条恢复链。
        assertFalse(
            LongPlaybackHealthPolicy.isFrozen(
                positionMoving = false,
                renderedNotIncreasingForMs = Spec.FREEZE_MS * 2,
            ),
        )
    }

    @Test fun `a decode freeze degrades the engine instead of re-picking the same source`() {
        // 「位置在前进、帧数不增」= 渲染/解码管线死了。这里最关键的修正：
        // 旧实现把冻结也丢进 recoverPlaybackStall 的通用阶梯，而那条阶梯第 0 步是
        // RELOAD_CURRENT（同一条线原地重试）——对解码冻结毫无意义，还白等一轮。
        // 冻结必须直接降级引擎：换源对同编码内容无效。
        val frozen = LongPlaybackHealthPolicy.action(
            bufferingMs = 0L,              // 冻结与 BUFFERING 无关：状态常是 READY
            bufferGrowing = true,          // buffer 在涨也不代表能出画面——渲染管死了
            positionMoving = true,         // 时钟照走，正是冻结的特征
            hasBackup = true,
            errorClass = PlaybackTelemetry.ErrorClass.DECODE,
        )
        assertEquals(
            "解码类冻结必须直接降级引擎，不能被「buffer 在涨」判成 HEALTHY",
            LongPlaybackHealthPolicy.Action.DEGRADE_ENGINE,
            frozen,
        )
    }
}

package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.playback.PlaybackRecoveryPolicy

object PlaybackPolicy { const val SEEK_STEP_MS = 10_000L }

/** Pure acceleration policy for a held VOD direction key. */
internal object SeekAccelerationPolicy {
    const val ACCELERATION_INTERVAL_MS = 500L
    const val INPUT_GAP_RESET_MS = 750L
    const val MAX_EXPONENT = 6

    data class State(
        val direction: Int = 0,
        val holdStartedAtMs: Long = -1L,
        val lastInputAtMs: Long = -1L,
    )

    data class Step(val deltaMs: Long, val state: State)

    /** Direction is -1 for rewind or +1 for forward. A new press or direction resets to 10s. */
    fun nextStep(
        direction: Int,
        repeatCount: Int,
        eventTimeMs: Long,
        state: State,
        baseStepMs: Long = PlaybackPolicy.SEEK_STEP_MS,
    ): Step? {
        if (direction != -1 && direction != 1) return null
        if (baseStepMs <= 0L) return null

        val timeWentBackwards = state.lastInputAtMs >= 0L && eventTimeMs < state.lastInputAtMs
        val gap = if (state.lastInputAtMs >= 0L && !timeWentBackwards) eventTimeMs - state.lastInputAtMs else Long.MAX_VALUE
        val reset = repeatCount <= 0 || state.direction != direction ||
            state.holdStartedAtMs < 0L || gap > INPUT_GAP_RESET_MS || timeWentBackwards
        val holdStartedAtMs = if (reset) eventTimeMs else state.holdStartedAtMs
        val heldMs = (eventTimeMs - holdStartedAtMs).coerceAtLeast(0L)
        val exponent = (heldMs / ACCELERATION_INTERVAL_MS).coerceIn(0L, MAX_EXPONENT.toLong()).toInt()
        val multiplier = 1L shl exponent
        val magnitude = if (baseStepMs > Long.MAX_VALUE / multiplier) Long.MAX_VALUE else baseStepMs * multiplier
        val nextState = State(direction, holdStartedAtMs, eventTimeMs)
        return Step(if (direction < 0) -magnitude else magnitude, nextState)
    }
}

/** Accumulates seek input independently of the engine's slower position mirror. */
internal class PendingSeekTargetState {
    var targetMs: Long? = null
        private set

    fun setTarget(targetMs: Long, durationMs: Long): Long? {
        if (durationMs <= 1_000L) return null
        return targetMs.coerceIn(0L, durationMs - 1_000L).also { this.targetMs = it }
    }

    fun addDelta(
        enginePositionMs: Long,
        durationMs: Long,
        deltaMs: Long,
        minimumTargetMs: Long = 0L,
    ): Long? {
        // Ignore unknown, transient and sub-second durations. In particular, never collapse a
        // seek to zero just because a provider briefly reports a tiny positive duration.
        if (durationMs <= 1_000L) return null
        // Keep the existing one-second tail guard so a forward seek cannot immediately end a VOD.
        val maxTarget = durationMs - 1_000L
        val minTarget = minimumTargetMs.coerceIn(0L, maxTarget)
        val base = (targetMs ?: enginePositionMs).coerceIn(minTarget, maxTarget)
        val candidate = when {
            deltaMs > 0L && base > Long.MAX_VALUE - deltaMs -> Long.MAX_VALUE
            deltaMs < 0L && base < Long.MIN_VALUE - deltaMs -> Long.MIN_VALUE
            else -> base + deltaMs
        }
        return candidate.coerceIn(minTarget, maxTarget).also { targetMs = it }
    }

    fun targetOrNull(): Long? = targetMs

    fun acknowledgeIfReached(positionMs: Long, toleranceMs: Long): Boolean {
        val target = targetMs ?: return false
        val lower = if (target > toleranceMs) target - toleranceMs else 0L
        val upper = if (target > Long.MAX_VALUE - toleranceMs) Long.MAX_VALUE else target + toleranceMs
        if (positionMs in lower..upper) {
            targetMs = null
            return true
        }
        return false
    }

    fun clear() {
        targetMs = null
    }
}

/** Suppresses only heuristic stall recovery around a user seek; real engine errors stay live. */
internal object SeekRecoveryGracePolicy {
    // User seeks can leave the decoder buffering for longer than the ordinary 8s stall watchdog.
    const val GRACE_MS = 15_000L

    fun shouldDefer(nowMs: Long, lastSeekAtMs: Long, keyHeld: Boolean): Boolean {
        if (keyHeld) return true
        if (lastSeekAtMs < 0L) return false
        val elapsed = nowMs - lastSeekAtMs
        return elapsed < 0L || elapsed < GRACE_MS
    }

    fun resumePosition(pendingTargetMs: Long?, currentPositionMs: Long): Long =
        pendingTargetMs ?: PlaybackRecoveryPolicy.resumePosition(currentPositionMs)
}

/** A transient engine error during an ordinary VOD seek gets one same-source restart. */
internal object SeekFailureRecoveryPolicy {
    fun shouldRetrySameSource(
        isLive: Boolean,
        isFootballVod: Boolean,
        pendingTargetMs: Long?,
        retryAlreadyUsed: Boolean,
    ): Boolean = !isLive && !isFootballVod && pendingTargetMs != null && !retryAlreadyUsed
}

/** A failed startup visits each source once; only an explicit retry starts a new round. */
object VodSourceAttemptPolicy {
    fun next(lineCount: Int, activeLine: Int, tried: Set<Int>, allowed: (Int) -> Boolean): Int? =
        (0 until lineCount).firstOrNull { it != activeLine && it !in tried && allowed(it) }
}

/** Remembers a VOD source by a stable provider line id instead of its mutable display index. */
object VodSourceSelectionPolicy {
    fun identity(line: LineInfo?): String? {
        if (line == null) return null
        val sourceLineId = line.playRef.split('|').getOrNull(1)?.takeIf(String::isNotBlank)
        if (sourceLineId != null) return "playRef:${line.label.trim()}|$sourceLineId"
        return line.vodId.takeIf(String::isNotBlank)?.let { "vodId:$it" }
    }

    fun matchingIndex(lines: List<LineInfo>, sourceIdentity: String?): Int? {
        if (sourceIdentity.isNullOrBlank()) return null
        return lines.indexOfFirst { identity(it) == sourceIdentity }.takeIf { it >= 0 }
    }

    fun initialIndex(lines: List<LineInfo>, rememberedIdentity: String?, preferredIndex: Int): Int =
        matchingIndex(lines, rememberedIdentity)
            ?: preferredIndex.takeIf { it in lines.indices }
            ?: 0
}

/** Decide whether a completed item should continue with the next VOD episode. */
object AutoPlayPolicy {
    fun nextEpisodeIndex(isLive: Boolean, isMovie: Boolean, currentIndex: Int, episodeCount: Int): Int? =
        if (!isLive && !isMovie && currentIndex >= 0 && currentIndex + 1 < episodeCount) currentIndex + 1 else null
}

object EpisodeMenuPolicy {
    const val RANGE_SIZE = 10
    fun hasRanges(count: Int) = count >= 20
    fun rangeLabels(count: Int): List<String> = if (!hasRanges(count)) emptyList() else
        (0 until (count + RANGE_SIZE - 1) / RANGE_SIZE).map { page ->
            val from = page * RANGE_SIZE + 1
            val to = minOf(count, from + RANGE_SIZE - 1)
            "第${from}-${to}集"
        }
    fun episodeLabel(index: Int) = (index + 1).toString()

    /** 面板选集网格的展示范围（2026-09-19 集数不全修复）：<20 集全量展示——
     *  此时分页 chip 不渲染，若仍按 10 集/页切片，落在第 2 页时前 10 集永远
     *  够不着（《深渊无间》16 集只见 11-16 的报障）；≥20 集才按页切片。 */
    fun visibleRange(count: Int, page: Int): IntRange =
        if (hasRanges(count)) {
            val start = page * RANGE_SIZE
            start until minOf(count, start + RANGE_SIZE)
        } else {
            0 until count
        }
}

object VisibleVodLinePolicy {
    fun apply(lines: List<LineInfo>): List<LineInfo> = lines.mapIndexed { index, line ->
        line.copy(label = line.label.trim().ifBlank { "信号源${index + 1}" })
    }
}

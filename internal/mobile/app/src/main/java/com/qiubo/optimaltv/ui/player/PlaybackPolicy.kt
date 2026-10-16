package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.playback.PlaybackRecoveryPolicy

object PlaybackPolicy {
    const val SEEK_STEP_MS = 10_000L
    /** 长按画面中央的临时倍速（移动端）：按住 = 该档，松手回用户倍速。
     *  网页版 tools/backend-src/www/app.js 同名常量 LP_SPEED 同口径。 */
    const val LONG_PRESS_SPEED = 3.0f
}

/** Keeps user-issued seek intent available while the player's position mirror catches up. */
internal class PendingSeekTarget {
    var targetMs: Long? = null
        private set

    fun base(positionMs: Long): Long = targetMs ?: positionMs.coerceAtLeast(0L)

    fun update(positionMs: Long) {
        targetMs = positionMs.coerceAtLeast(0L)
    }

    fun clear() {
        targetMs = null
    }
}

/** Pure rules for the mobile player's press-and-hold seek gesture and its recovery window. */
internal object LongPressSeekPolicy {
    const val MIN_DURATION_MS = 1_000L
    const val TICK_MS = 200L
    const val BASE_STEP_MS = 1_000L
    const val ACCELERATION_PERIOD_MS = 1_000L
    const val MAX_STEP_MS = 32_000L
    const val SEEK_REQUEST_INTERVAL_MS = 750L
    // Covers the mobile 8s stall detector plus scheduling/position-mirror lag after a seek.
    const val RECOVERY_GRACE_MS = 15_000L
    const val POSITION_CONFIRMATION_TOLERANCE_MS = 2_000L

    /** A 1s step per 200ms tick, doubling each second, capped at 32s per tick. */
    fun stepMs(heldMs: Long): Long {
        val stage = (heldMs.coerceAtLeast(0L) / ACCELERATION_PERIOD_MS).coerceAtMost(5L).toInt()
        return (BASE_STEP_MS shl stage).coerceAtMost(MAX_STEP_MS)
    }

    /** Return null when the stream has no useful seek range or the request is invalid. */
    fun target(
        currentMs: Long,
        durationMs: Long,
        direction: Int,
        stepMs: Long,
        minimumTargetMs: Long = 0L,
    ): Long? {
        if (durationMs < MIN_DURATION_MS || direction == 0 || stepMs <= 0L) return null
        val maxMs = (durationMs - 1_000L).coerceAtLeast(0L)
        val minMs = minimumTargetMs.coerceIn(0L, maxMs)
        val current = currentMs.coerceIn(minMs, maxMs)
        val target = if (direction > 0) {
            if (stepMs > maxMs - current) maxMs else current + stepMs
        } else {
            if (stepMs > current - minMs) minMs else current - stepMs
        }
        return target.coerceIn(minMs, maxMs)
    }

    /** Zero means dispatch now; otherwise callers should wait and replace older queued targets. */
    fun dispatchDelayMs(lastDispatchAtMs: Long, nowMs: Long): Long {
        if (lastDispatchAtMs <= 0L) return 0L
        val elapsed = (nowMs - lastDispatchAtMs).coerceAtLeast(0L)
        return (SEEK_REQUEST_INTERVAL_MS - elapsed).coerceAtLeast(0L)
    }

    fun shouldDeferStallRecovery(holdActive: Boolean, lastSeekAtMs: Long, nowMs: Long): Boolean {
        if (holdActive) return true
        return lastSeekAtMs > 0L && nowMs >= lastSeekAtMs && nowMs - lastSeekAtMs < RECOVERY_GRACE_MS
    }

    fun positionConfirmsTarget(positionMs: Long, targetMs: Long, fromMs: Long? = null): Boolean {
        if (kotlin.math.abs(positionMs - targetMs) > POSITION_CONFIRMATION_TOLERANCE_MS) return false
        // A small forward seek must not be considered complete while the old position mirror
        // is still visible. Require the engine clock to have moved in the requested direction.
        val origin = fromMs ?: return true
        return when {
            targetMs > origin -> positionMs > origin
            targetMs < origin -> positionMs < origin
            else -> true
        }
    }
}

/** Recovery policy shared by seek-error and stall recovery call sites. */
internal object SeekRecoveryPolicy {
    fun restorePosition(pendingTargetMs: Long?, currentPositionMs: Long): Long =
        pendingTargetMs?.coerceAtLeast(0L) ?: PlaybackRecoveryPolicy.resumePosition(currentPositionMs)

    /** Seek-triggered same-line recovery is limited to ordinary VOD; live/football keep their ladder. */
    fun keepSelectedLine(isLive: Boolean, isFootballVod: Boolean, pendingTargetMs: Long?): Boolean =
        !isLive && !isFootballVod && pendingTargetMs != null
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
        (0 until (count + RANGE_SIZE - 1) / RANGE_SIZE).map { p -> "第${p * RANGE_SIZE + 1}-${minOf(count, (p + 1) * RANGE_SIZE)}集" }
    fun episodeLabel(index: Int) = (index + 1).toString()
    /** 面板选集网格的展示范围（2026-09-19 集数不全修复，与 TV 版同源）：<20 集全量
     *  展示——分页 chip 不显示时按页切片会让前 10 集够不着；≥20 集才按页切片。 */
    fun visibleRange(count: Int, page: Int): IntRange =
        if (hasRanges(count)) {
            val start = page * RANGE_SIZE
            start until minOf(count, start + RANGE_SIZE)
        } else {
            0 until count
        }
}
object VisibleVodLinePolicy {
    fun apply(lines: List<LineInfo>) = lines.mapIndexed { i, line ->
        if (line.label.isBlank()) line.copy(label = "信号源${i + 1}") else line
    }
}

/** Stable point-in-time source selection for VOD; the list index can change between episodes. */
internal object VodSourcePreferencePolicy {
    fun identity(line: LineInfo?): String? {
        if (line == null) return null
        val refId = line.playRef.split('|').getOrNull(1)?.trim().orEmpty()
        if (refId.isNotBlank()) return "ref:$refId"
        return line.vodId.trim().takeIf(String::isNotBlank)?.let { "vod:$it" }
            ?: line.label.substringBefore(" · ").trim().takeIf(String::isNotBlank)?.let { "label:$it" }
    }

    /** Exact ref wins; if episode-specific IDs changed, retain the same provider when possible. */
    fun preferredIndex(lines: List<LineInfo>, rememberedIdentity: String?): Int? {
        val remembered = rememberedIdentity?.takeIf(String::isNotBlank) ?: return null
        lines.indexOfFirst { identity(it) == remembered }.takeIf { it >= 0 }?.let { return it }

        val rememberedProvider = providerId(remembered) ?: return null
        return lines.indexOfFirst { line ->
            providerId(identity(line).orEmpty()) == rememberedProvider
        }.takeIf { it >= 0 }
    }

    /** Direct/sibling sources use their stable vod ID; resolver-backed lines use source ID. */
    private fun providerId(identity: String): String? = when {
        identity.startsWith("ref:") -> identity.removePrefix("ref:").substringBefore(':').takeIf(String::isNotBlank)
        identity.startsWith("vod:") -> identity.removePrefix("vod:").takeIf(String::isNotBlank)
        identity.startsWith("label:") -> identity.removePrefix("label:").takeIf(String::isNotBlank)
        else -> null
    }
}

/** Failure recovery may rotate live sources, but VOD always stays on the selected source. */
internal object AutomaticSourceSwitchPolicy {
    fun maySwitch(isLive: Boolean, isFootballVod: Boolean = false): Boolean = isLive || isFootballVod
}

internal object PlaybackStartupTimeoutPolicy {
    const val VOD_START_WATCHDOG_MS = 300_000L
    const val LIVE_OR_FOOTBALL_START_WATCHDOG_MS = 20_000L

    fun timeoutMs(isLive: Boolean, isFootballVod: Boolean): Long =
        if (isLive || isFootballVod) LIVE_OR_FOOTBALL_START_WATCHDOG_MS else VOD_START_WATCHDOG_MS
}

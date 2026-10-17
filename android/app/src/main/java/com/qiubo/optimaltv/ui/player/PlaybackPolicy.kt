package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.model.LineInfo

object PlaybackPolicy { const val SEEK_STEP_MS = 10_000L }

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

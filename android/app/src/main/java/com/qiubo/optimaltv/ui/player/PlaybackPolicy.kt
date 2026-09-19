package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.model.LineInfo

object PlaybackPolicy { const val SEEK_STEP_MS = 10_000L }

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
    fun apply(lines: List<LineInfo>): List<LineInfo> = lines.take(5).mapIndexed { index, line ->
        line.copy(label = "信号源${index + 1}")
    }
}

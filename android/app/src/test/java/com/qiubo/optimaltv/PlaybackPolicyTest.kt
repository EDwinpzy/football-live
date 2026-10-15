package com.qiubo.optimaltv

import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.ui.player.EpisodeMenuPolicy
import com.qiubo.optimaltv.ui.player.AutoPlayPolicy
import com.qiubo.optimaltv.ui.player.PlaybackPolicy
import com.qiubo.optimaltv.ui.player.VisibleVodLinePolicy
import com.qiubo.optimaltv.ui.player.VodSourceAttemptPolicy
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PlaybackPolicyTest {
    @Test fun unavailableSourcesStopAfterOneRoundAndCanBeRetriedExplicitly() {
        val tried = mutableSetOf(0)
        var active = 0
        val visited = mutableListOf(active)
        repeat(17) {
            val next = VodSourceAttemptPolicy.next(17, active, tried) { true }
            if (next != null) {
                active = next
                tried += next
                visited += next
            }
        }
        assertEquals((0 until 17).toList(), visited)
        assertEquals(null, VodSourceAttemptPolicy.next(17, active, tried) { true })
        tried.clear()
        assertEquals(0, VodSourceAttemptPolicy.next(17, active, tried) { true })
    }

    @Test fun blockedOrCurrentSourceIsNotAnAutomaticFallback() {
        assertEquals(null, VodSourceAttemptPolicy.next(1, 0, setOf(0)) { true })
        assertEquals(2, VodSourceAttemptPolicy.next(3, 0, setOf(0)) { it != 1 })
        assertEquals(null, VodSourceAttemptPolicy.next(3, 0, setOf(0, 2)) { it != 1 })
    }

    @Test fun episodeMenuUsesTwentyBoundaryAndTenEpisodeRanges() {
        assertFalse(EpisodeMenuPolicy.hasRanges(19))
        assertTrue(EpisodeMenuPolicy.hasRanges(20))
        assertEquals(listOf("第1-10集", "第11-20集"), EpisodeMenuPolicy.rangeLabels(20))
        assertEquals("3", EpisodeMenuPolicy.episodeLabel(2))
    }

    @Test fun episodeMenuVisibleRangeShowsAllBelowTwenty() {
        // 2026-09-19 集数不全修复（《深渊无间》16 集面板只见 11-16）：
        // <20 集不分页——即使当前集落在"第 2 页"（page=1），也必须全量可见
        assertEquals(0 until 16, EpisodeMenuPolicy.visibleRange(16, page = 1))
        assertEquals(0 until 19, EpisodeMenuPolicy.visibleRange(19, page = 1))
        assertEquals(0 until 10, EpisodeMenuPolicy.visibleRange(10, page = 0))
        // ≥20 集按 10 集/页切片
        assertEquals(0 until 10, EpisodeMenuPolicy.visibleRange(25, page = 0))
        assertEquals(10 until 20, EpisodeMenuPolicy.visibleRange(25, page = 1))
        assertEquals(20 until 25, EpisodeMenuPolicy.visibleRange(25, page = 2))
    }

    @Test fun vodLinesKeepProviderNamesAndGiveBlankLinesAFallback() {
        val lines = (1..8).map { LineInfo("v$it", "private-$it", "u$it") }
        assertEquals((1..8).map { "private-$it" }, VisibleVodLinePolicy.apply(lines).map { it.label })
        assertEquals("信号源1", VisibleVodLinePolicy.apply(listOf(LineInfo("v", "  ", "u"))).single().label)
    }

    @Test fun seekStepIsTenSeconds() = assertEquals(10_000L, PlaybackPolicy.SEEK_STEP_MS)

    @Test fun autoPlayAdvancesOnlyToTheNextVodEpisode() {
        assertEquals(3, AutoPlayPolicy.nextEpisodeIndex(isLive = false, isMovie = false, currentIndex = 2, episodeCount = 4))
        assertEquals(null, AutoPlayPolicy.nextEpisodeIndex(isLive = false, isMovie = false, currentIndex = 3, episodeCount = 4))
        assertEquals(null, AutoPlayPolicy.nextEpisodeIndex(isLive = true, isMovie = false, currentIndex = 2, episodeCount = 4))
        assertEquals(null, AutoPlayPolicy.nextEpisodeIndex(isLive = false, isMovie = true, currentIndex = 0, episodeCount = 2))
    }
}

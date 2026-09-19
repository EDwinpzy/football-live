package com.qiubo.optimaltv

import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.ui.player.EpisodeMenuPolicy
import com.qiubo.optimaltv.ui.player.AutoPlayPolicy
import com.qiubo.optimaltv.ui.player.PlaybackPolicy
import com.qiubo.optimaltv.ui.player.VisibleVodLinePolicy
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PlaybackPolicyTest {
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

    @Test fun vodLinesExposeEveryAnonymousSignal() {
        val lines = (1..8).map { LineInfo("v$it", "private-$it", "u$it") }
        assertEquals(listOf("信号源1", "信号源2", "信号源3", "信号源4", "信号源5", "信号源6", "信号源7", "信号源8"),
            VisibleVodLinePolicy.apply(lines).map { it.label })
    }

    @Test fun seekStepIsTenSeconds() = assertEquals(10_000L, PlaybackPolicy.SEEK_STEP_MS)

    @Test fun autoPlayAdvancesOnlyToTheNextVodEpisode() {
        assertEquals(3, AutoPlayPolicy.nextEpisodeIndex(isLive = false, isMovie = false, currentIndex = 2, episodeCount = 4))
        assertEquals(null, AutoPlayPolicy.nextEpisodeIndex(isLive = false, isMovie = false, currentIndex = 3, episodeCount = 4))
        assertEquals(null, AutoPlayPolicy.nextEpisodeIndex(isLive = true, isMovie = false, currentIndex = 2, episodeCount = 4))
        assertEquals(null, AutoPlayPolicy.nextEpisodeIndex(isLive = false, isMovie = true, currentIndex = 0, episodeCount = 2))
    }
}

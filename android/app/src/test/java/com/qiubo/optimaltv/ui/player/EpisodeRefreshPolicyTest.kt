package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EpisodeRefreshPolicyTest {

    @Test
    fun selectingTheCurrentVodLineIsAValidRetryButLivePickerKeepsItsNoopBehavior() {
        assertTrue(ManualLineSelectionPolicy.shouldStart(lineCount = 1, activeLine = 0, selectedLine = 0, isLive = false))
        assertFalse(ManualLineSelectionPolicy.shouldStart(lineCount = 2, activeLine = 0, selectedLine = 0, isLive = true))
        assertFalse(ManualLineSelectionPolicy.shouldStart(lineCount = 1, activeLine = 0, selectedLine = 1, isLive = false))
    }

    @Test
    fun acceptsNewEpisodesWithoutMovingCurrentEpisode() {
        assertTrue(episodes(current = 12, incoming = 14))
    }

    @Test
    fun acceptsNewLineEvenWhenEpisodeCountIsUnchanged() {
        assertTrue(episodes(current = 12, incoming = 12, currentLines = 1, incomingLines = 2))
    }

    @Test
    fun acceptsRenamedEpisodesAtTheSameCount() {
        assertTrue(EpisodeRefreshPolicy.accepts(
            currentNames = listOf("第1集", "第2集"),
            incomingNames = listOf("第1集", "正片"),
            currentLines = 1,
            incomingLines = 1,
            keepIndex = 1,
        ))
    }

    @Test
    fun rejectsUnchangedPayloadSoTheListIsNotRewritten() {
        assertFalse(episodes(current = 12, incoming = 12))
    }

    @Test
    fun rejectsTruncatedEpisodeLists() {
        assertFalse(episodes(current = 12, incoming = 8))
    }

    @Test
    fun acceptsCurrentSourceEpisodesEvenWhenAnotherSourceHadMore() {
        assertTrue(EpisodeRefreshPolicy.accepts(
            currentNames = (1..14).map { "第${it}集" },
            incomingNames = (1..13).map { "第${it}集" },
            currentLines = 2,
            incomingLines = 2,
            keepIndex = 7,
            sameEpisodeSource = false,
        ))
    }

    @Test
    fun rejectsARefreshThatBelongsToAnOlderMovieOrPlaybackLine() {
        val request = EpisodeRefreshTicket(
            requestId = 3,
            vodId = "hhkan:721",
            detailRef = "hhkan:721",
            playbackGeneration = 8,
            episodeIndex = 4,
            activeLine = 1,
            sourceIdentity = "线路甲|23",
        )

        assertFalse(EpisodeRefreshPolicy.isCurrentRequest(
            request, latestRequestId = 4, vodId = "hhkan:721", detailRef = "hhkan:721",
            playbackGeneration = 8, episodeIndex = 4, activeLine = 1, sourceIdentity = "线路甲|23",
        ))
        assertFalse(EpisodeRefreshPolicy.isCurrentRequest(
            request, latestRequestId = 3, vodId = "hhkan:999", detailRef = "hhkan:999",
            playbackGeneration = 9, episodeIndex = 4, activeLine = 1, sourceIdentity = "线路甲|23",
        ))
        assertFalse(EpisodeRefreshPolicy.isCurrentRequest(
            request, latestRequestId = 3, vodId = "hhkan:721", detailRef = "hhkan:721",
            playbackGeneration = 8, episodeIndex = 4, activeLine = 1, sourceIdentity = "线路乙|24",
        ))
    }

    @Test
    fun rejectsEpisodeListThatNoLongerCoversThePlayingEpisode() {
        assertFalse(episodes(current = 1, incoming = 3, keepIndex = 3))
    }

    private fun episodes(
        current: Int,
        incoming: Int,
        currentLines: Int = 1,
        incomingLines: Int = 1,
        keepIndex: Int = 7,
    ): Boolean = EpisodeRefreshPolicy.accepts(
        currentNames = (1..current).map { "第${it}集" },
        incomingNames = (1..incoming).map { "第${it}集" },
        currentLines = currentLines,
        incomingLines = incomingLines,
        keepIndex = keepIndex,
    )
}

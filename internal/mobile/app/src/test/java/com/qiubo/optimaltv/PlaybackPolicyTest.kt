package com.qiubo.optimaltv

import com.qiubo.optimaltv.ui.player.AutoPlayPolicy
import org.junit.Assert.assertEquals
import org.junit.Test

class PlaybackPolicyTest {
    @Test fun autoPlayAdvancesOnlyToTheNextVodEpisode() {
        assertEquals(3, AutoPlayPolicy.nextEpisodeIndex(isLive = false, isMovie = false, currentIndex = 2, episodeCount = 4))
        assertEquals(null, AutoPlayPolicy.nextEpisodeIndex(isLive = false, isMovie = false, currentIndex = 3, episodeCount = 4))
        assertEquals(null, AutoPlayPolicy.nextEpisodeIndex(isLive = true, isMovie = false, currentIndex = 2, episodeCount = 4))
        assertEquals(null, AutoPlayPolicy.nextEpisodeIndex(isLive = false, isMovie = true, currentIndex = 0, episodeCount = 2))
    }
}

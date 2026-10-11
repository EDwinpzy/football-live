package com.qiubo.optimaltv.ui.live

import com.qiubo.optimaltv.data.model.MatchItem
import org.junit.Assert.assertEquals
import org.junit.Test

class LiveMatchVisibilityTest {
    @Test
    fun finishedMatchesAreHiddenWhileLiveAndUpcomingRemainVisible() {
        fun match(id: String, status: String) = MatchItem(
            matchId = id,
            home = "甲队",
            away = "乙队",
            league = "英超",
            status = status,
        )
        val live = match("live", "live")
        val upcoming = match("upcoming", "upcoming")
        val finished = match("finished", "finished")

        assertEquals(
            listOf(live, upcoming),
            visibleLiveScheduleMatches(listOf(live, finished, upcoming)),
        )
    }
}

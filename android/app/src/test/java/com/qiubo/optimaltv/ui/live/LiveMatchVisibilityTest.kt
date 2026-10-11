package com.qiubo.optimaltv.ui.live

import com.qiubo.optimaltv.data.model.MatchItem
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.util.Calendar
import java.util.TimeZone

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

    @Test
    fun beijingClockPromotesUpcomingButNeverFinishesOrOverridesHeldStates() {
        val beijing = TimeZone.getTimeZone("Asia/Shanghai")
        val start = Calendar.getInstance(beijing).apply {
            clear()
            set(2026, Calendar.DECEMBER, 31, 23, 50, 0)
        }.timeInMillis
        val previousZone = TimeZone.getDefault()
        try {
            TimeZone.setDefault(TimeZone.getTimeZone("America/Los_Angeles"))
            val kickoff = MatchItem(
                matchId = "year-boundary",
                home = "甲队",
                away = "乙队",
                league = "英超",
                date = "12-31",
                dateFull = "2026-12-31",
                time = "23:50",
                status = "upcoming",
            )
            assertEquals(start, matchStartMillis(kickoff, start + 20 * 60_000L))
            assertEquals("live", MatchClockPolicy.project(kickoff, start, start + 149 * 60_000L).status)
            assertEquals("upcoming", MatchClockPolicy.project(kickoff, start, start + 150 * 60_000L).status)
            assertEquals("live", MatchClockPolicy.project(kickoff.copy(status = "live"), start, start + 180 * 60_000L).status)
            assertEquals("finished", MatchClockPolicy.project(kickoff.copy(status = "finished"), start, start + 20 * 60_000L).status)
            val postponed = kickoff.copy(status = "postponed")
            assertEquals("postponed", MatchClockPolicy.project(postponed, start, start + 240 * 60_000L).status)
            assertEquals("延期", postponed.displayStatus)
            assertEquals("upcoming", MatchClockPolicy.project(kickoff.copy(status = "live"), start, start - 60_000L).status)
        } finally {
            TimeZone.setDefault(previousZone)
        }
    }

    @Test
    fun staleSnapshotCannotReplaceAMatchListAlreadyReturnedByFreshRequest() {
        val cached = listOf(MatchItem("cached", "旧主队", "旧客队", league = "英超"))
        val fresh = LiveUiState(loading = false, matches = listOf(MatchItem("fresh", "新主队", "新客队")))

        assertNull(snapshotFallbackState(fresh, cached))
        assertEquals(cached, snapshotFallbackState(LiveUiState(), cached)?.matches)
    }
}

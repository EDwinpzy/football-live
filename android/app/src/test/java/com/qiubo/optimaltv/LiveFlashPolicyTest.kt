package com.qiubo.optimaltv

import com.qiubo.optimaltv.data.model.MatchItem
import com.qiubo.optimaltv.ui.live.ImportantTeamPolicy
import com.qiubo.optimaltv.ui.live.MatchStatusKind
import com.qiubo.optimaltv.ui.live.TodayMatchPager
import com.qiubo.optimaltv.ui.live.matchStatusKind
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LiveFlashPolicyTest {
    private fun match(id: Int, home: String = "甲队", away: String = "乙队", league: String = "英超") =
        MatchItem(id.toString(), home, away, league)

    @Test fun todayPagerFillsFirstRowBeforeSecond() {
        val five = TodayMatchPager.pages((1..5).map(::match))
        assertEquals(listOf(1, 2, 3, 4), five.first().rows[0].map { it.matchId.toInt() })
        assertEquals(listOf(5), five.first().rows[1].map { it.matchId.toInt() })
        assertEquals(listOf(4, 4), TodayMatchPager.pages((1..8).map(::match)).first().rows.map { it.size })
        assertEquals(2, TodayMatchPager.pages((1..9).map(::match)).size)
    }

    @Test fun importantMeansPopularTeamsNotWholeLeague() {
        assertTrue(ImportantTeamPolicy.isImportant(match(1, "曼彻斯特联", "布莱顿")))
        assertTrue(ImportantTeamPolicy.isImportant(match(2, "皇家马德里", "赫塔费", "西甲")))
        assertFalse(ImportantTeamPolicy.isImportant(match(3, "上海申花", "北京国安", "中超")))
        assertFalse(ImportantTeamPolicy.isImportant(match(4, "布伦特福德", "伯恩利", "英超")))
    }

    @Test fun legacyEndedAndCanonicalFinishedStayFinishedOnCards() {
        val legacy = match(5).copy(status = "ended")
        val canonical = match(6).copy(status = "finished")
        assertEquals(MatchStatusKind.FINISHED, matchStatusKind(legacy))
        assertEquals(MatchStatusKind.FINISHED, matchStatusKind(canonical))
        assertFalse(legacy.isLive)
        assertFalse(canonical.isLive)
    }

    @Test fun finishedMatchWithoutScoreDoesNotBecomeUpcoming() {
        val finished = match(7).copy(status = "finished", time = "20:00")
        assertEquals(MatchStatusKind.FINISHED, matchStatusKind(finished))
        assertFalse(finished.hasScore)
    }
}

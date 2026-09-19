package com.qiubo.optimaltv.ui.live

import com.qiubo.optimaltv.data.model.MatchItem

enum class MatchStatusKind { LIVE, UPCOMING, FINISHED }

fun matchStatusKind(match: MatchItem): MatchStatusKind = when {
    match.isLive -> MatchStatusKind.LIVE
    match.isFinished -> MatchStatusKind.FINISHED
    else -> MatchStatusKind.UPCOMING
}

data class TodayMatchPage(val rows: List<List<MatchItem>>)
object TodayMatchPager { fun pages(items: List<MatchItem>) = items.chunked(8).map { TodayMatchPage(listOf(it.take(4), it.drop(4))) } }
object ImportantTeamPolicy {
    private val teams = listOf("曼联","曼彻斯特联","曼城","曼彻斯特城","利物浦","阿森纳","切尔西","热刺","皇家马德里","皇马","巴塞罗那","巴萨","马德里竞技","拜仁","多特蒙德","国际米兰","国米","AC米兰","尤文图斯","巴黎圣日耳曼","那不勒斯","罗马","勒沃库森")
    private val nations = Regex("世界杯|欧洲杯|美洲杯|亚洲杯|欧国联|世预赛|欧预赛|国家队")
    fun isImportant(m: MatchItem) = teams.any { m.home.contains(it, true) || m.away.contains(it, true) } || nations.containsMatchIn(m.league)
}
object MatchClockPolicy {
    fun project(m: MatchItem, start: Long?, now: Long): MatchItem {
        if (m.isFinished || start == null) return m
        val e = ((now - start) / 60_000).toInt(); if (e < 0) return m
        if (e >= 110) return m.copy(status = "finished")
        val played = if (e <= 45) e else if (e <= 60) 45 else e - 15
        return m.copy(status = "live", minute = played.coerceIn(0, 90).toString())
    }
}

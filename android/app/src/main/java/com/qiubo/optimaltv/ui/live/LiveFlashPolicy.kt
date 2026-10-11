package com.qiubo.optimaltv.ui.live

import com.qiubo.optimaltv.data.model.MatchItem

enum class MatchStatusKind { LIVE, UPCOMING, FINISHED }

fun matchStatusKind(match: MatchItem): MatchStatusKind = when {
    match.isLive -> MatchStatusKind.LIVE
    match.isFinished -> MatchStatusKind.FINISHED
    else -> MatchStatusKind.UPCOMING
}

data class TodayMatchPage(val rows: List<List<MatchItem>>)

object TodayMatchPager {
    private const val COLUMNS = 4
    private const val PAGE_SIZE = 8
    fun pages(items: List<MatchItem>): List<TodayMatchPage> = items.chunked(PAGE_SIZE).map { page ->
        TodayMatchPage(listOf(page.take(COLUMNS), page.drop(COLUMNS)))
    }
}

object ImportantTeamPolicy {
    private val aliases = listOf(
        setOf("曼联", "曼彻斯特联"), setOf("曼城", "曼彻斯特城"), setOf("利物浦"),
        setOf("阿森纳"), setOf("切尔西"), setOf("热刺", "托特纳姆热刺"),
        setOf("皇马", "皇家马德里"), setOf("巴萨", "巴塞罗那"), setOf("马竞", "马德里竞技"),
        setOf("拜仁", "拜仁慕尼黑"), setOf("多特", "多特蒙德"),
        setOf("国际米兰", "国米"), setOf("AC米兰", "米兰"), setOf("尤文", "尤文图斯"),
        setOf("巴黎圣日耳曼", "巴黎"), setOf("那不勒斯"), setOf("罗马"), setOf("勒沃库森")
    ).flatten()
    private val majorNational = Regex("世界杯|欧洲杯|美洲杯|亚洲杯|欧国联|世预赛|欧预赛|国家队")
    fun isImportant(match: MatchItem): Boolean =
        aliases.any { match.home.contains(it, true) || match.away.contains(it, true) } ||
            majorNational.containsMatchIn(match.league)
}

object MatchClockPolicy {
    private val extraTimeLeague = Regex("杯|淘汰|决赛|半决赛|世界杯|欧洲杯|亚洲杯|美洲杯|欧冠|欧联|欧协|世俱杯|友谊赛")

    fun project(match: MatchItem, startMillis: Long?, nowMillis: Long): MatchItem {
        if (match.isFinished || match.isClockHeld || startMillis == null) return match
        val elapsed = ((nowMillis - startMillis) / 60_000L).toInt()
        if (elapsed < 0) return if (match.isLive) match.copy(status = "upcoming", minute = "") else match
        // The backend owns terminal-state decisions, including authoritative 7m
        // results. A device clock may only move an upcoming fixture to live.
        val finishGrace = if (extraTimeLeague.containsMatchIn(match.league)) 210 else 150
        if (!match.isUpcoming || elapsed >= finishGrace) return match
        val played = when { elapsed <= 45 -> elapsed; elapsed <= 60 -> 45; else -> elapsed - 15 }
        return match.copy(status = "live", minute = played.coerceIn(0, 90).toString())
    }
}

package com.qiubo.optimaltv.data.model

/** Resolved playable line for a live football match. */
data class LiveSourceLine(val label: String, val url: String)

// ---------- 做直播（原版 /api/matches 等，1:1 复刻字段） ----------

/** /api/matches 提供的信号元数据；可播放线路在进入播放器后由 /api/stream 解析并探活。 */
data class MatchChannel(
    val src: String,
    val id: String,
    val name: String,
)

/** A football stream returned by the backend after resolving and probing one source. */
data class LiveStreamSource(
    val source: String,
    val name: String,
    /** Playback-ready URL wrapped by the local relay. */
    val playbackUrl: String,
    /** Source-specific page referer retained for renewal requests. */
    val referer: String,
)

data class MatchItem(
    val matchId: String,
    val home: String,
    val away: String,
    val league: String = "",
    val leagueColor: String = "",
    val date: String = "",        // MM-DD
    val time: String = "",        // 开赛时间或分钟数
    val status: String = "",      // live / upcoming / finished（兼容旧 ended）
    val minute: String = "",
    val homeScore: String = "",   // score.h，空 = 无比分
    val awayScore: String = "",
    val channels: List<MatchChannel> = emptyList(),   // 比赛信号元数据，不是已解析探活的播放地址
    val dateFull: String = "",   // YYYY-MM-DD 北京时间日期，用于精确开赛时间计算
) {
    /** 上游旧快照曾写 ended；统一由模型归一，避免卡片/点击链路各自判断出不同状态。 */
    val isLive: Boolean get() = status.trim().equals("live", ignoreCase = true)
    val isUpcoming: Boolean get() = status.trim().equals("upcoming", ignoreCase = true)
    val isClockHeld: Boolean get() = status.trim().lowercase() in setOf(
        "postponed", "delayed", "pending", "cancelled", "canceled", "abandoned",
        "延期", "推迟", "待定", "取消", "腰斩",
    )
    val displayStatus: String get() = when (status.trim().lowercase()) {
        "live" -> "直播中"
        "upcoming" -> "未开赛"
        "finished", "ended", "close", "ft", "完场", "完赛", "已结束", "已完赛", "赛后" -> "完场"
        "postponed", "delayed", "延期", "推迟" -> "延期"
        "pending", "待定" -> "待定"
        "cancelled", "canceled", "取消" -> "取消"
        "abandoned", "腰斩" -> "腰斩"
        else -> status
    }
    val isFinished: Boolean get() = status.trim().equals("finished", ignoreCase = true) ||
        status.trim().equals("ended", ignoreCase = true) ||
        status.trim().equals("close", ignoreCase = true)
    val hasScore: Boolean get() = homeScore.isNotEmpty() && awayScore.isNotEmpty()
}

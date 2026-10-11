package com.qiubo.optimaltv.data.model

data class Category(val id: String, val name: String)

data class Episode(val index: Int, val name: String, val url: String)

/** Stable row read from the versioned metadata SQLite catalogue. */
data class CatalogMedia(
    val mytvId: String,
    val mediaType: String,
    val title: String,
    val originalTitle: String = "",
    val year: String = "",
    val updatedAt: Long = 0L,
    val overview: String = "",
    val posterUrl: String = "",
    val rating: Double = 0.0,
    val popularity: Double = 0.0,
    val genres: List<String> = emptyList(),
    val regions: List<String> = emptyList(),
    val externalIds: Map<String, String> = emptyMap(),
    val actors: List<String> = emptyList(),
    val directors: List<String> = emptyList(),
    val searchTerms: List<String> = emptyList(),
) {
    val legacyRef: String? get() = externalIds["douban"]?.takeIf(String::isNotBlank)?.let { "douban:$it" }
}

enum class SourceAvailability {
    MATCHING, AVAILABLE, UNAVAILABLE;

    companion object {
        fun fromWire(value: String): SourceAvailability = when (value) {
            "available", "ready" -> AVAILABLE
            "unavailable" -> UNAVAILABLE
            else -> MATCHING
        }
    }
}

/** 一条可换线路（跨源同名影片的同集地址，或 hhkan 站内线路的懒解析引用） */
data class LineInfo(
    val vodId: String,
    val label: String,
    val url: String,
    /** 非空 = 播放时经原版后端 /hhkan/play/{ref} 实时解析直链（url 置空） */
    val playRef: String = "",
)

/**
 * 影片条目。id = "{sourceId}:{rawId}" 全局唯一。
 * normTitle 用于跨源聚合同一部影片 → 详情页把它作为「线路」展示（技术方案 §3.2 线路记忆）。
 * detailRef 非空 = 懒解析源（hhkan）：目录只有元数据，选集/直链在详情与起播时经原版后端解析。
 */
data class VodItem(
    val id: String,
    val sourceId: String,
    val title: String,
    val categoryId: String,
    val year: String = "",
    val area: String = "",
    val rating: Double = 0.0,
    val desc: String = "",
    val posterUrl: String = "",
    /** 影视页顶部 hero 用的大图：豆瓣列表只给 ~240 宽的 s_ratio_poster，铺满屏会糊，
     *  后端 hero_url 给的是同一张图的 /view/photo/large/public/（1142×1600）。空则回落到 posterUrl。 */
    val heroUrl: String = "",
    val tags: List<String> = emptyList(),
    val episodes: List<Episode> = emptyList(),
    val detailRef: String = "",
    /** 片库角标（原版 vcard .remark：TC/正片/更新至高清） */
    val remark: String = "",
    /** 详情元信息串（hhkan meta：「2026 / 美国 / 爱情片」，时间地区类型一行） */
    val meta: String = "",
    /** 演员表（详情接口 actors，已清洗为「A / B / C」） */
    val actors: String = "",
    /** 元数据中心的永久 ID；本地用户记录仍以原有 [id] 为键。 */
    val mytvId: String = "",
    /** 目录索引字段，仅用于内存搜索；不会写入用户进度或收藏表。 */
    val searchTerms: List<String> = emptyList(),
) {
    /** 归一化标题：去空白 + 全角转半角小写，用于跨源分组 */
    val normTitle: String by lazy {
        title.trim().replace(Regex("\\s+"), "").lowercase()
    }

    fun vodKey(epIndex: Int): String =
        if (episodes.size > 1) "$id-e${epIndex + 1}" else id
}

fun formatTime(ms: Long): String {
    if (ms <= 0) return "00:00"
    val totalSec = ms / 1000
    val h = totalSec / 3600
    val m = (totalSec % 3600) / 60
    val s = totalSec % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%02d:%02d".format(m, s)
}

/** 片库角标归一（需求 影视#4）：「第30集已完结」→「全30集」，其余原样 */
fun normalizeRemark(remark: String): String {
    val r = remark.trim()
    return Regex("^第(\\d+)集?已完结$").find(r)?.let { "全${it.groupValues[1]}集" } ?: r
}

// ---------- 足球直播（原版 /api/matches 等，1:1 复刻字段） ----------

/** 比赛信号通道（/api/matches channels：该场当前可用的足球直播线路）。
 *  src=请求标识，id=专属频道数字段，name=该赛事对应的线路显示名。 */
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
    val channels: List<MatchChannel> = emptyList(),   // 该场动态可用信号线路（空=无可选线路）
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

/** 回看战报（/api/football-results） */
data class ResultItem(
    val home: String,
    val away: String,
    val h: String,
    val a: String,
    val title: String = "",
    val url: String = "",
)

/** 集锦/回放视频（/api/football-videos，id = hhkan vid） */
data class VideoItem(
    val id: String,
    val title: String,
    val cover: String = "",
    val league: String = "",
    /** 两队名（后端从标题解析；无则空） */
    val home: String = "",
    val away: String = "",
    /** 比分（后端匹配到对应场次才填，否则空） */
    val h: String = "",
    val a: String = "",
)

/** 足球新闻（/api/football-news） */
data class NewsItem(
    val title: String,
    val img: String = "",
    val time: String = "",
    val content: String = "",
)

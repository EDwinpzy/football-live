package com.qiubo.optimaltv.data.model

data class Category(val id: String, val name: String)

data class Episode(val index: Int, val name: String, val url: String)

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

/** 比赛信号通道（/api/matches channels：站点每场比赛的动态线路组合）。
 *  src=请求标识（bb/plu/qqlive15..33/666…编号随时轮换）、id=专属频道数字段
 *  （qqlive 公共线为空串，后端不用）、name=站点原始命名（已剥「(无插件)」与尾部数字）。 */
data class MatchChannel(
    val src: String,
    val id: String,
    val name: String,
)

data class MatchItem(
    val matchId: String,
    val home: String,
    val away: String,
    val league: String = "",
    val leagueColor: String = "",
    val date: String = "",        // MM-DD
    val time: String = "",        // 开赛时间或分钟数
    val status: String = "",      // live / upcoming / ended
    val minute: String = "",
    val homeScore: String = "",   // score.h，空 = 无比分
    val awayScore: String = "",
    val channels: List<MatchChannel> = emptyList(),   // 该场动态信号线路（空=旧数据/快照，回退默认源表）
) {
    val isLive: Boolean get() = status == "live"
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

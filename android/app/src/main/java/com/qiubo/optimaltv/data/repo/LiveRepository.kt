package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.OtvLog
import com.qiubo.optimaltv.data.model.LiveStreamSource
import com.qiubo.optimaltv.data.model.MatchChannel
import com.qiubo.optimaltv.data.model.MatchItem
import com.qiubo.optimaltv.data.model.NewsItem
import com.qiubo.optimaltv.data.model.ResultItem
import com.qiubo.optimaltv.data.model.VideoItem
import com.qiubo.optimaltv.data.prefs.SettingsStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.net.URLEncoder

/**
 * 足球直播仓库（1:1 复刻原版 web 数据链路，同一后端）：
 * - matches: /api/matches（90 场左右，含 live 比分）
 * - 直播信号: /api/stream/{matchId}?src=all&fresh=1 → 仅返回已解析并探活的 HLS 线路
 * - 回看: /api/football-results（战报比分）→ 播放走 hhkan 搜索回放
 * - 集锦: /api/football-videos（hhkan vid，7 天内）
 * - 新闻: /api/football-news
 */
class LiveRepository(
    private val settings: SettingsStore,
    private val okHttp: OkHttpClient,
    private val appContext: android.content.Context,
) {

    companion object {
        /** 直播源由服务端提供对应 Referer，供中继播放清单及分片。 */
        fun relayUrl(base: String, raw: String, referer: String = "https://www.88zhibo.tv/"): String =
            "$base/api/relay?u=" + URLEncoder.encode(raw, "UTF-8") + "&ref=" + URLEncoder.encode(referer, "UTF-8")

        private const val TAG = "OTV"

        /** 足球 hero 海报池大小（2026-08-29 换新海报：tools/build_backend_zip.py 写入 fb-01..36.jpg） */
        const val POSTER_POOL = 36

        /** 原版 hero 重要度：live+1000 / 有比分+100 / 五大联赛+50 / 欧战国赛+40。
         *  v1.16：中超并入头部联赛权重（用户需求「增加中超球队的比赛」） */
        private val TOP_LEAGUE_RE = Regex("^(英超|西甲|意甲|德甲|法甲|中超|足协杯)")
        private val NAT_RE = Regex("欧冠|欧联|欧协|欧国联|世预赛|欧预赛|欧洲杯|亚洲杯|美洲杯|世界杯|友谊赛|国家队")

        /**
         * 主流赛事白名单（2026-08-28 用户需求「过滤掉非主流比赛」后的收紧版）：
         * 仅保留五大联赛、欧战俱乐部赛事、国家队大赛、传统豪门杯赛与荷甲/葡超/中超。
         * 罗甲/沙特联/巴西杯/阿根廷杯/J联赛/墨联/各类男篮女篮等小众赛事全部剔除。
         */
        private val NON_FOOTBALL_RE = Regex("男篮|女篮|篮球|排球|网球|琼斯杯|NBL|NBA|CBA|WNBA|冰球|棒球|橄榄球|乒乓|羽毛")
        private val EURO_CLUB_CUP_RE = Regex("欧冠|欧联|欧协|欧会|欧超|世俱|欧国联|欧罗巴")
        /** 亚洲俱乐部赛事（2026-09-15 补）：亚冠精英赛 / 亚冠联2——中超球队常年参赛，
         *  收紧白名单漏掉它之后，「今日比赛」在只有亚冠比赛的日子会整块空掉
         *  （实测 09-15：当天 4 场全是亚冠，界面显示「暂无比赛」，其中 2 场正在直播）。 */
        private val ASIA_CLUB_CUP_RE = Regex("亚冠")
        private val MAJOR_LEAGUE_RE = Regex("^(英超|西甲|意甲|德甲|法甲|荷甲|葡超|中超)")
        /** 中国赛事通道（v1.16 用户需求「增加中超球队的比赛」）：中超联赛+足协杯（参赛队=中超球队） */
        private val CHINA_COMP_RE = Regex("中超|足协杯")
        private val MAJOR_CUP_RE = Regex("^(英联杯|足总杯|社区盾|国王杯|西班牙超级杯|西超杯|意大利杯|意超杯|德国杯|德超杯|法国杯|法超杯|超级杯)")
        private val NAT_COMP_RE = Regex("世预赛|欧预赛|欧国联|欧洲杯|美洲杯|亚洲杯|世界杯|友谊赛|国家队|亚运|奥运会|金杯赛|非洲杯|麒麟杯")
        fun heroImportance(m: MatchItem): Int {
            var s = 0
            if (m.isLive) s += 1000
            if (m.hasScore) s += 100
            if (TOP_LEAGUE_RE.containsMatchIn(m.league)) s += 50
            if (NAT_RE.containsMatchIn(m.league)) s += 40
            return s
        }
    }

    private var matchesCache: Pair<Long, List<MatchItem>>? = null

    /** 冷抓接口（/hhkan/search 站点实时抓取）专用：75s 读超时——后端 _fetch 会在
     *  镜像域间切换重试（每域最长 25s×挑战求解），45s 会把「慢而能成」的搜索截成空结果（需求 搜索#2） */
    private val slowHttp by lazy {
        okHttp.newBuilder().readTimeout(75, java.util.concurrent.TimeUnit.SECONDS).build()
    }

    private suspend fun getSlow(base: String, path: String): JSONObject = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url(base.trimEnd('/') + path)
            .header("User-Agent", "Mozilla/5.0 (Linux; Android 11) OptimalTV/1.0")
            .build()
        slowHttp.newCall(req).execute().use { resp ->
            require(resp.isSuccessful) { "HTTP ${resp.code} $path" }
            JSONObject(resp.body?.string().orEmpty())
        }
    }

    private suspend fun get(base: String, path: String): JSONObject = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url(base.trimEnd('/') + path)
            .header("User-Agent", "Mozilla/5.0 (Linux; Android 11) OptimalTV/1.0")
            .build()
        okHttp.newCall(req).execute().use { resp ->
            require(resp.isSuccessful) { "HTTP ${resp.code} $path" }
            JSONObject(resp.body?.string().orEmpty())
        }
    }

    /** baseUrl 内存缓存（v1.19 流畅度）：UI 组合期 teamIconUrl/posterUrl 高频调用
     *  （足球页每张比赛卡 2 次队标 + hero 海报），旧版每次 runBlocking 读 DataStore
     *  ——主线程组合期几十次阻塞等待，光标移动一顿一顿的直接来源。
     *  后台收集 DataStore flow：首发射加载 + 设置页变更即时刷新，语义与旧版一致。 */
    @Volatile private var cachedBase: String = "http://127.0.0.1:8090"

    init {
        kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO).launch {
            runCatching {
                settings.settings.collect { cachedBase = it.hhkanBaseUrl.trim().trimEnd('/') }
            }
        }
    }

    private fun baseUrl(): String = cachedBase

    /** 内置后端就绪探测：app 内置 proxy 冷启动需几秒，首次拉数据前先轮询 /api/health。
     *  外置后端(非 127.0.0.1)或已连通则立即返回。 */
    private suspend fun waitBackendReady(base: String) {
        if (!base.contains("127.0.0.1") && !base.contains("localhost")) return
        repeat(30) {
            runCatching {
                val req = Request.Builder().url("$base/api/health").build()
                okHttp.newCall(req).execute().use { if (it.isSuccessful) return }
            }
            kotlinx.coroutines.delay(500)
        }
    }

    /** 原版足球过滤表（isMainstreamMatch 依赖，assets/football_filters.json） */
    private data class FootballFilters(
        val top5: Set<String>,
        val national: Set<String>,
        val natKeywords: List<String>,
        val cups: List<String>,
    )

    private val filters: FootballFilters by lazy {
        runCatching {
            val o = org.json.JSONObject(appContext.assets.open("football_filters.json").bufferedReader().use { it.readText() })
            fun arr(k: String): Set<String> = buildSet {
                o.optJSONArray(k)?.let { a -> for (i in 0 until a.length()) add(a.getString(i)) }
            }
            FootballFilters(arr("TOP5_TEAMS"), arr("NATIONAL_TEAMS"), arr("NAT_MATCH_KEYWORDS").toList(), arr("MAIN_CUPS").toList())
        }.getOrDefault(FootballFilters(emptySet(), emptySet(), emptyList(), emptyList()))
    }

    /** 主流赛事判定（2026-08-28 收紧）：剔除篮球/小联赛/非主流杯赛，
     *  仅保留五大联赛+荷甲葡超中超、欧战、国家队大赛、传统豪门杯赛（杯赛卡双知名球队）。 */
    fun isMainstreamMatch(m: MatchItem): Boolean {
        val f = filters
        val L = m.league
        // 非足球赛事整体剔除（男篮世预赛/女篮等此前混入）
        if (NON_FOOTBALL_RE.containsMatchIn(L) ||
            NON_FOOTBALL_RE.containsMatchIn(m.home) || NON_FOOTBALL_RE.containsMatchIn(m.away)
        ) return false
        if (TOP_LEAGUE_RE.containsMatchIn(L)) return true
        if (MAJOR_LEAGUE_RE.containsMatchIn(L)) return true
        if (CHINA_COMP_RE.containsMatchIn(L)) return true
        // 欧战（欧冠/欧联/欧协等）：参赛队遍布各国联赛，直接保留
        if (EURO_CLUB_CUP_RE.containsMatchIn(L)) return true
        // 亚洲俱乐部赛事（亚冠精英赛/亚冠联2）：同为洲际赛事，含中超球队，直接保留
        if (ASIA_CLUB_CUP_RE.containsMatchIn(L)) return true
        // 国家队大赛（世预赛/欧国联/友谊赛/亚洲杯…）
        if (NAT_COMP_RE.containsMatchIn(L)) return true
        // 传统豪门杯赛：需至少一方知名球队（top5 表 + 国家队表）
        if (MAJOR_CUP_RE.containsMatchIn(L)) {
            val hTop = m.home in f.top5; val aTop = m.away in f.top5
            val hNat = m.home in f.national; val aNat = m.away in f.national
            return (hTop || hNat) && (aTop || aNat)
        }
        return false
    }

    private fun todayTomorrowKeys(): Pair<String, String> {
        val fmt = java.text.SimpleDateFormat("MM-dd", java.util.Locale.CHINA)
        val cal = java.util.Calendar.getInstance()
        val today = fmt.format(cal.time)
        cal.add(java.util.Calendar.DAY_OF_MONTH, 1)
        return today to fmt.format(cal.time)
    }

    /** 比赛列表（v1.23：30s 缓存对齐后端 CACHE_TTL——前端 15s/25s 轮询下 60s 本地
     *  缓存会把开赛上屏延迟吞回去；只保留足球 + 今日/明日，失败抛异常由 UI 显示错误态） */
    suspend fun matches(force: Boolean = false): List<MatchItem> = withContext(Dispatchers.IO) {
        matchesCache?.takeIf { !force && System.currentTimeMillis() - it.first < 30_000 }?.second?.let { return@withContext it }
        waitBackendReady(baseUrl())
        // 内置后端冷抓比赛数据可能较慢，首次拉取可能超时：失败则 sleep 重试几次再抛
        var jarr: org.json.JSONArray? = null
        var lastErr: Exception? = null
        for (i in 0..3) {
            try { jarr = get(baseUrl(), "/api/matches").getJSONArray("matches"); break }
            catch (e: Exception) { lastErr = e; kotlinx.coroutines.delay(4000) }
        }
        if (jarr == null) throw lastErr ?: RuntimeException("matches 拉取失败")
        val arr = jarr
        val (today, tomorrow) = todayTomorrowKeys()
        // optString 对 JSON null 会返回字面量 "null"（需求 足球#1「null'」根因之一）——统一消毒
        fun os(o: org.json.JSONObject, k: String): String =
            if (o.isNull(k)) "" else o.optString(k).takeUnless { it == "null" } ?: ""
        val list = (0 until arr.length()).mapNotNull { i ->
            val o = arr.optJSONObject(i) ?: return@mapNotNull null
            val sc = o.optJSONObject("score")
            fun sc2(k: String): String =
                if (sc == null || sc.isNull(k)) "" else sc.optString(k).takeUnless { it == "null" } ?: ""
            // 列表频道只作目录元信息；播放页从 /api/stream?src=all 获取已探活线路。
            val chArr = o.optJSONArray("channels")
            val channels = (0 until (chArr?.length() ?: 0)).mapNotNull channel@ { ci ->
                val c = chArr?.optJSONObject(ci) ?: return@channel null
                fun cs(k: String): String = if (c.isNull(k)) "" else c.optString(k).takeUnless { it == "null" } ?: ""
                val src = cs("src")
                if (src.isBlank()) return@channel null
                MatchChannel(src = src, id = cs("id"), name = cs("name"))
            }
            MatchItem(
                matchId = os(o, "match_id"),
                home = os(o, "home"),
                away = os(o, "away"),
                league = os(o, "league"),
                leagueColor = os(o, "league_color"),
                date = os(o, "date"),
                time = os(o, "time"),
                status = os(o, "status"),
                minute = os(o, "minute"),
                homeScore = sc2("h"),
                awayScore = sc2("a"),
                channels = channels,
            )
        }.filter {
            it.matchId.isNotBlank() && it.home.isNotBlank() && it.away.isNotBlank()
        }
        // v1.16 防死页三级回退（用户报障「什么比赛都没了，光标也不动」）：
        // ① 常规 = 主流赛事 × 今日/明日；② 空则回退「全部主流赛事不限日期」（国际比赛日/
        //    赛程真空期页面不再死空）；③ 仍空（白名单全不命中）才回退全部足球比赛兜底。
        val mainstream = list.filter { isMainstreamMatch(it) }
        val finalList = when {
            mainstream.any { it.date == today || it.date == tomorrow } ->
                list.filter { isMainstreamMatch(it) && (it.date == today || it.date == tomorrow) }
            mainstream.isNotEmpty() -> {
                OtvLog.w("matches 今日/明日无主流赛事 → 回退全量主流 ${mainstream.size} 场（不限日期）")
                mainstream
            }
            else -> {
                OtvLog.w("matches 主流白名单零命中 → 回退全部足球 ${list.size} 场兜底")
                list
            }
        }
        matchesCache = System.currentTimeMillis() to finalList
        // v1.10 磁盘快照（低端机秒开）：下次启动立即恢复上屏，后台 refresh 替换
        writeMatchesSnapshot(finalList)
        finalList
    }

    // ---------- 比赛列表磁盘快照（v1.10 2026-08-31） ----------
    private fun snapshotFile(): java.io.File = java.io.File(appContext.filesDir, "matches_snapshot.json")

    /** 启动恢复上次比赛列表（null=无快照/损坏）；UI 立即上屏，后台刷新替换新数据 */
    fun matchesFromSnapshot(): List<MatchItem>? = runCatching {
        val f = snapshotFile()
        if (!f.exists() || f.length() == 0L) return null
        val arr = org.json.JSONArray(f.readText())
        (0 until arr.length()).mapNotNull { i ->
            val o = arr.optJSONObject(i) ?: return@mapNotNull null
            val chArr = o.optJSONArray("chs")
            val channels = (0 until (chArr?.length() ?: 0)).mapNotNull channel@ { ci ->
                val c = chArr?.optJSONObject(ci) ?: return@channel null
                val src = c.optString("s")
                if (src.isBlank()) return@channel null
                MatchChannel(src = src, id = c.optString("i"), name = c.optString("n"))
            }
            MatchItem(
                matchId = o.optString("id"), home = o.optString("h"), away = o.optString("a"),
                league = o.optString("l"), leagueColor = o.optString("lc"), date = o.optString("d"),
                time = o.optString("t"), status = o.optString("st"), minute = o.optString("m"),
                homeScore = o.optString("hs"), awayScore = o.optString("as"),
                channels = channels,
            ).takeIf { it.matchId.isNotBlank() }
        }.takeIf { it.isNotEmpty() }?.also {
            // 快照同时写入内存缓存（60s TTL）：冷启动快照态点卡进直播时 srcsFor
            // 也能拿到动态 channels（否则回退硬编码源表）；后台 refresh 到货后自然替换
            matchesCache = System.currentTimeMillis() to it
        }
    }.getOrNull()

    private fun writeMatchesSnapshot(list: List<MatchItem>) {
        runCatching {
            val arr = org.json.JSONArray()
            list.forEach {
                arr.put(
                    org.json.JSONObject()
                        .put("id", it.matchId).put("h", it.home).put("a", it.away)
                        .put("l", it.league).put("lc", it.leagueColor).put("d", it.date)
                        .put("t", it.time).put("st", it.status).put("m", it.minute)
                        .put("hs", it.homeScore).put("as", it.awayScore)
                        .put("chs", org.json.JSONArray().apply {
                            it.channels.forEach { c -> put(org.json.JSONObject().put("s", c.src).put("i", c.id).put("n", c.name)) }
                        }),
                )
            }
            // 临时文件 + 原子改名：写一半中断不留半个快照
            val dst = snapshotFile()
            val tmp = java.io.File(dst.parentFile, dst.name + ".tmp")
            tmp.writeText(arr.toString())
            if (!tmp.renameTo(dst)) {
                dst.delete()
                tmp.renameTo(dst)
            }
        }
    }

    /**
     * Get only server-resolved HLS lines that passed the upstream probe. The all-lines request is
     * intentionally made when opening every football player so stale match-list channels never
     * appear in the picker. Source order and labels come directly from the backend.
     */
    suspend fun liveStreamSources(matchId: String, fresh: Boolean = true): List<LiveStreamSource> =
        withContext(Dispatchers.IO) {
            runCatching {
                val base = baseUrl()
                val o = getSlow(base, LiveStreamRequestPath.build(matchId, "all", fresh))
                LiveStreamPayloadParser.available(o).map { payload ->
                    LiveStreamSource(
                        source = payload.source,
                        name = payload.name,
                        playbackUrl = relayUrl(base, payload.url, payload.referer),
                        referer = payload.referer,
                    )
                }
            }.onFailure { android.util.Log.w(TAG, "stream $matchId all 线路解析失败: ${it.message}") }
                .getOrDefault(emptyList())
        }

    /** Resolve one previously listed source again, preserving its source-specific referer if the
     *  renewal response omits it. Dynamic source IDs are URL-encoded instead of allowlisted. */
    suspend fun refreshLiveStreamSource(
        matchId: String,
        source: String,
        fresh: Boolean = true,
        fallbackName: String = "",
        fallbackReferer: String = "",
    ): LiveStreamSource? = withContext(Dispatchers.IO) {
        if (source.isBlank()) return@withContext null
        runCatching {
            val base = baseUrl()
            val payload = LiveStreamPayloadParser.renewed(
                getSlow(base, LiveStreamRequestPath.build(matchId, source, fresh)),
                requestedSource = source,
                fallbackName = fallbackName,
                fallbackReferer = fallbackReferer,
            ) ?: return@withContext null
            LiveStreamSource(
                source = payload.source,
                name = payload.name,
                playbackUrl = relayUrl(base, payload.url, payload.referer),
                referer = payload.referer,
            )
        }.onFailure { android.util.Log.w(TAG, "renew stream $source 失败: ${it.message}") }.getOrNull()
    }

    /** 直播续签健康检查（v1.14）：当前 relay 清单还能否拉到（HTTP 200=活）。
     *  用于「签名已轮换但旧地址仍在服务期 → 延后切换」——上游轮换常有宽限期，
     *  白白热切换 = 每 ~9 分钟一次 ~300ms 可感知冻结（直播「偶尔卡顿」来源之一）。
     *  网络异常保守视为「活」（防误切；死地址随后由卡顿触发 kick 续签兜底）。 */
    suspend fun playlistAlive(relayUrl: String): Boolean = withContext(Dispatchers.IO) {
        if (relayUrl.isBlank()) return@withContext false
        runCatching {
            val fast = okHttp.newBuilder()
                .callTimeout(8, java.util.concurrent.TimeUnit.SECONDS)
                .readTimeout(java.util.concurrent.TimeUnit.SECONDS.toMillis(8), java.util.concurrent.TimeUnit.MILLISECONDS)
                .build()
            fast.newCall(Request.Builder().url(relayUrl).build()).execute().use { resp ->
                val alive = resp.code == 200
                android.util.Log.i(TAG, "playlistAlive ${resp.code} → $alive")
                alive
            }
        }.getOrDefault(true)
    }

    /** 回看战报（近 16 条，含比分） */
    suspend fun results(): List<ResultItem> = withContext(Dispatchers.IO) {
        runCatching {
            val arr = get(baseUrl(), "/api/football-results?n=16").getJSONArray("list")
            (0 until arr.length()).mapNotNull { i ->
                val o = arr.optJSONObject(i) ?: return@mapNotNull null
                val h = o.optString("home"); val a = o.optString("away")
                if (h.isBlank() || a.isBlank() || !o.has("h")) return@mapNotNull null
                ResultItem(
                    home = h, away = a,
                    h = o.optInt("h", -1).let { if (it < 0) "" else it.toString() },
                    a = o.optInt("a", -1).let { if (it < 0) "" else it.toString() },
                    title = o.optString("title"), url = o.optString("url"),
                )
            }
        }.getOrDefault(emptyList())
    }

    /** 集锦（hhkan 聚合，id 即 hhkan vid，可直接走 hhkan 详情解析播放） */
    suspend fun videos(): List<VideoItem> = withContext(Dispatchers.IO) {
        runCatching {
            val base = baseUrl()
            val arr = get(base, "/api/football-videos?n=48").getJSONArray("list")
            (0 until arr.length()).mapNotNull { i ->
                val o = arr.optJSONObject(i) ?: return@mapNotNull null
                val id = o.optLong("id", 0L)
                if (id <= 0L) return@mapNotNull null
                // cover 来自 vres.* CDN，模拟器/真机不可直连 → 经后端 /hhkan/proxy 中继，否则封面不显示
                val cover = com.qiubo.optimaltv.data.source.HhkanSource.relayed(base, o.optString("cover"))
                VideoItem(
                    id = id.toString(), title = o.optString("title"), cover = cover,
                    league = o.optString("league"),
                    home = o.optString("home"), away = o.optString("away"),
                    h = o.optString("h"), a = o.optString("a"),
                )
            }
        }.getOrDefault(emptyList())
    }

    /** 足球新闻 */
    suspend fun news(): List<NewsItem> = withContext(Dispatchers.IO) {
        runCatching {
            val arr = get(baseUrl(), "/api/football-news?n=40").getJSONArray("list")
            (0 until arr.length()).mapNotNull { i ->
                val o = arr.optJSONObject(i) ?: return@mapNotNull null
                NewsItem(title = o.optString("title"), img = o.optString("img"), time = o.optString("time"), content = o.optString("content"))
            }
        }.getOrDefault(emptyList())
    }

    /** 队标（原版 iconUrl 1:1：normTeamName 归一化 → 本地 574 队映射零请求，未命中回落后端接口） */
    private val teamIcons: Map<String, String> by lazy {
        runCatching {
            val json = appContext.assets.open("team_icons.json").bufferedReader().use { it.readText() }
            val o = JSONObject(json)
            buildMap { o.keys().forEach { k -> put(k, o.getString(k)) } }
        }.getOrDefault(emptyMap())
    }

    /** normTeamName（原版：剥赛事/轮次前缀 + 空白归一） */
    fun normTeamName(name: String): String {
        var n = name.trim().replace(Regex("\\s+"), "")
        // 赛事+轮次前缀（如「英超第18轮南安普顿」「欧冠1/4决赛阿森纳」）
        n = n.replace(Regex("^(英超|西甲|意甲|德甲|法甲|中超|欧冠|欧联|欧协|欧国联|世预赛|亚冠|足协杯|英联杯|足总杯|德乙|英冠|荷甲|葡超|比甲|苏超|日职|韩K|沙特联|巴甲|阿甲|美职足)[\\s\\S]*?第?[\\d一二三四五六七八九十]+[轮轮次回合]"), "")
        n = n.replace(Regex("^(英超|西甲|意甲|德甲|法甲|中超|欧冠|欧联|欧协|欧国联|世预赛|亚冠)[\\s\\S]{0,6}?(?=[^\\x00-\\x7F])"), "")
        return n
    }

    fun teamIconUrl(team: String): String {
        val base = baseUrl()
        val norm = normTeamName(team)
        // 2026-08-28：本地映射表全部改为绝对 URL（api-sports/TDB CDN，国内可达），
        // 旧版相对路径 assets/teams/*.png 在内置后端 zip 里不存在 → 全量 404 → 队标大面积缺失
        val local = teamIcons[team] ?: teamIcons[norm] ?: teamIcons[baseTeamName(norm)]
        if (local != null) {
            return if (local.startsWith("http")) local
            else "$base/${local.removePrefix("./")}"
        }
        return "$base/api/team-icon-img?name=" + URLEncoder.encode(team, "UTF-8")
    }

    /** 别名兜底：去常见后缀再查一次 */
    private fun baseTeamName(n: String): String =
        n.replace(Regex("(女篮|男篮|女足|男足|B队|二队|U\\d+|青年队)$"), "")

    /** 实时搜索（原版 doSearch：每次输入防抖后调 /hhkan/search，站点原生支持拼音节匹配如 shouhu；冷抓 45s） */
    suspend fun search(k: String): List<VideoItem> = withContext(Dispatchers.IO) {
        val q = k.trim()
        if (q.isEmpty()) return@withContext emptyList()
        runCatching {
            val arr = getSlow(baseUrl(), "/hhkan/search?k=" + URLEncoder.encode(q, "UTF-8")).optJSONArray("items")
            (0 until (arr?.length() ?: 0)).mapNotNull { i ->
                val o = arr!!.optJSONObject(i) ?: return@mapNotNull null
                val id = o.optLong("id", 0L)
                if (id <= 0L) return@mapNotNull null
                VideoItem(id = id.toString(), title = o.optString("title"), cover = o.optString("cover"))
            }
        }.onFailure { android.util.Log.w(TAG, "search 失败: ${it.message}") }.getOrDefault(emptyList())
    }

    /** 足球海报池（2026-08-29 换新海报 36 张 jpg，经后端静态托管） */
    fun posterUrl(index: Int): String =
        baseUrl() + "/assets/football/fb-" + String.format(java.util.Locale.ROOT, "%02d", (index % POSTER_POOL) + 1) + ".jpg"

    /** 启动海报索引（需求 足球#2）：进程内随机一次固定——hero 左右切比赛海报不换，
     *  仅每次启动 app 时换一张（旧版按 matchId 散列，切比赛海报跟着换，用户不要） */
    val launchPosterIndex: Int = kotlin.random.Random.nextInt(POSTER_POOL)

    /** 设备本地豆瓣媒体库搜索。retryNonce 仅保留二进制兼容，搜索不会触网也无需重试。 */
    suspend fun searchRemote(query: String, retryNonce: Int = 0): List<com.qiubo.optimaltv.data.model.VodItem> = withContext(Dispatchers.IO) {
        runCatching {
            val base = baseUrl()
            val k = URLEncoder.encode(query.trim(), "UTF-8")
            val arr = getSlow(base, "/api/search?q=$k&limit=60").optJSONArray("items")
            (0 until (arr?.length() ?: 0)).mapNotNull { i ->
                val o = arr?.optJSONObject(i) ?: return@mapNotNull null
                val id = o.optString("douban_id").ifBlank { o.optString("id").removePrefix("douban:") }
                if (id.isBlank()) return@mapNotNull null
                com.qiubo.optimaltv.data.model.VodItem(
                    id = "douban:$id", sourceId = "douban",
                    title = o.optString("title").trim(),
                    categoryId = "douban:" + o.optString("category", "movie"),
                    year = o.optString("year"), rating = o.optDouble("rating", 0.0),
                    desc = o.optString("summary"),
                    posterUrl = com.qiubo.optimaltv.data.source.VodApiSource.relayPoster(base, o.optString("poster_url")),
                    detailRef = id,
                )
            }.filter { it.title.isNotBlank() }
        }.getOrDefault(emptyList())
    }

    /** 已知 hhkan vid 的线路解析（集锦直入播放器用） */
    suspend fun hhkanVidLines(vid: String): List<com.qiubo.optimaltv.data.model.LineInfo> = withContext(Dispatchers.IO) {
        val base = baseUrl()
        runCatching {
            val d = com.qiubo.optimaltv.data.source.HhkanSource.fetchDetail(base, okHttp, vid)
            d.lines.mapNotNull { line ->
                line.episodes.getOrNull(0)?.let { ep ->
                    com.qiubo.optimaltv.data.model.LineInfo(
                        vodId = "hhkanplay:$vid", label = line.name, url = "",
                        playRef = "$vid-${ep.second}-${ep.third}",
                    )
                }
            }
        }.getOrDefault(emptyList())
    }

    /**
     * 回放解析（原版 playRewatch）：hhkan 搜「home VS away」→ 命中取详情多线路；
     * 搜不到 → 集锦列表同队名兜底。返回 (标题, 线路列表)，null = 无回放。
     */
    suspend fun replayLines(home: String, away: String): Pair<String, List<com.qiubo.optimaltv.data.model.LineInfo>>? =
        withContext(Dispatchers.IO) {
            val base = baseUrl()
            var vid = ""; var title = ""
            runCatching {
                val kw = URLEncoder.encode("$home VS $away", "UTF-8")
                val arr = get(base, "/hhkan/search?k=$kw").optJSONArray("items")
                if (arr != null) for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    val t = o.optString("title")
                    if (t.contains(home) || t.contains(away)) { vid = o.optLong("id").toString(); title = t; break }
                }
            }
            if (vid.isBlank()) {
                videos().find { it.title.contains(home) || it.title.contains(away) }?.let { vid = it.id; title = it.title }
            }
            if (vid.isBlank()) return@withContext null
            val d = com.qiubo.optimaltv.data.source.HhkanSource.fetchDetail(base, okHttp, vid)
            val lines = d.lines.mapIndexedNotNull { idx, line ->
                line.episodes.getOrNull(0)?.let { ep ->
                    com.qiubo.optimaltv.data.model.LineInfo(
                        vodId = "replay:$vid", label = line.name, url = "",
                        playRef = "$vid-${ep.second}-${ep.third}",
                    )
                }
            }
            if (lines.isEmpty()) null else (title.ifBlank { "$home 对阵 $away 回放" }) to lines
        }
}

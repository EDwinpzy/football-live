package com.qiubo.optimaltv.data.source

import com.qiubo.optimaltv.data.model.Category
import com.qiubo.optimaltv.data.model.CatalogMedia
import com.qiubo.optimaltv.data.model.VodItem
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder

/**
 * 云端 Resolver 的一条候选线路引用（不含任何播放地址）。
 *
 * 关键契约：[sourceId]:[contentId] 与 `/vod/detail` 下发线路的 `id`
 * （`"{sourceId}:{contentId}:{epIndex}"`）前两段同构——Resolver 是既有可播线路之上的
 * **排序层**，不是新的取流通道。播放地址仍由 `/vod/play` 即时解析。
 */
data class ResolvedSource(
    val sourceId: String,
    val contentId: String,
    val sourceName: String = "",
    val episodeIndex: Int = 0,
    val matchScore: Double = 0.0,
) {
    /** 与 detail 线路 id 对齐的前缀，用于把候选对上具体线路。 */
    val linePrefix: String get() = "$sourceId:$contentId"
}

/**
 * Resolver 结果。status 与后端 `RESOLVE_STATUSES` 一一对应。
 *
 * 只有 MATCHED / STALE_AVAILABLE 视为可用；其余（含 TEMPORARILY_UNAVAILABLE）
 * **必须回落到 `/vod/detail`**——端侧实测能正常匹配出 12 条线路（约 0.9s），
 * 云端拿不到结果时绝不能把功能改坏成空片源。
 */
data class SourceResolveResult(
    val mytvId: Long,
    val episode: Int,
    val status: String,
    val preferred: ResolvedSource? = null,
    val backups: List<ResolvedSource> = emptyList(),
    val cacheKind: String = "",
    val cacheAgeMs: Long = 0L,
) {
    /** 是否可直接用于起播。 */
    val usable: Boolean
        get() = (status == "MATCHED" || status == "STALE_AVAILABLE") && preferred != null

    /** 有没有任何候选（哪怕状态不佳，也能给用户一条可试的线）。 */
    val hasCandidates: Boolean
        get() = preferred != null || backups.isNotEmpty()

    /** 诊断用：云端说了什么、缓存命中什么类型。 */
    val diagnostic: String get() = "status=$status cache=$cacheKind/${cacheAgeMs}ms"
}

/**
 * Resolver 候选 → 已有 detail 线路的重排（**纯函数，不碰网络/DB**）。
 *
 * 单独抽出来的理由：这段排序是「用户点播放时最先决定用哪条线」的地方，
 * 错一位就是把观众从最优源推到次优源，而它又是纯计算——没必要留在挂网络 IO 的
 * 仓库方法里，只能靠真机点播才能发现。抽出后单测直接锁住顺序契约。
 *
 * 契约（顺序敏感，逐条都有代价）：
 * 1. `preferred` 永远第一——它就是后端算出的最优匹配，被挤后等于优化白做；
 * 2. `backups` 保持后端给的次序追加在 preferred 之后（**不倒序插入**）；
 * 3. 没被点名的 detail 线路按原顺序接在最后——Resolver 只是排序层，不删线路。
 */
object ResolverRanking {

    /**
     * 返回重排后的下标序列（相对入参 [linePrefixes] 的原下标）。
     *
     * [linePrefixes] 是每条 detail 线路的 `"{sourceId}:{contentId}"`（前两段），
     * 调用方从 `playRef` 里解析。这样本函数不依赖 LineInfo / 网络，可直接单测。
     */
    fun order(
        linePrefixes: List<String>,
        preferred: ResolvedSource?,
        backups: List<ResolvedSource>,
    ): List<Int> {
        val lineCount = linePrefixes.size
        if (lineCount == 0) return emptyList()
        val picked = LinkedHashSet<Int>()
        // preferred 在前，backups 按给定次序在后。用 addLast 语义（List.add）
        // 而不是 insert(0, …)：后者会把首个 backup 顶到 preferred 前面。
        val order = ArrayList<ResolvedSource>(backups.size + 1)
        preferred?.let { order.add(it) }
        order.addAll(backups)
        for (candidate in order) {
            if (picked.size >= lineCount) break
            val idx = (0 until lineCount).firstOrNull { index ->
                index !in picked && matchesCandidate(
                    linePrefixes[index], candidate.sourceId, candidate.contentId,
                )
            }
            if (idx != null) picked.add(idx)
        }
        // 未被点名的线路按原顺序补齐，保证「不丢线路」。
        (0 until lineCount).forEach { if (it !in picked) picked.add(it) }
        return picked.toList()
    }

    /**
     * 候选与某条 detail 线路前缀是否同一条线路。
     *
     * detail 线路 id 形如 `{sourceId}:{contentId}:{epIndex}`，候选前缀是前两段
     * `"{sourceId}:{contentId}"`。必须按**段**比较而不是整串 startsWith：
     * `content-1` 会前缀命中 `content-10`，
     * 把观众送到另一集的线路上（错集，且往往是不可播的那条）。
     */
    fun matchesCandidate(linePrefix: String, sourceId: String, contentId: String): Boolean {
        if (linePrefix.isBlank()) return false
        val parts = linePrefix.split(':')
        if (parts.size < 2) return false
        return parts[0] == sourceId && parts[1] == contentId
    }
}

/** Device-local Douban catalogue exposed by the embedded Python VOD API. */
class VodApiSource(
    private val baseUrl: String,
    private val okHttp: OkHttpClient,
) : ContentSource {
    override val id: String = "douban"
    override val displayName: String = "豆瓣"

    override suspend fun snapshot(): SourceSnapshot = snapshot(refresh = false)

    suspend fun snapshot(refresh: Boolean): SourceSnapshot = withContext(Dispatchers.IO) {
        val categories = CATEGORIES.map { Category("douban:${it.first}", it.second) }
        val items = coroutineScope {
            CATEGORIES.map { (key, _) ->
                async {
                    runCatching {
                        val json = fetchJson(baseUrl, VodRoutes.home(key, refresh), okHttp)
                        val sections = json.optJSONArray("sections")
                        (0 until (sections?.length() ?: 0)).flatMap { i ->
                            parseItems(sections?.optJSONObject(i)?.optJSONArray("items"), key, baseUrl)
                        }
                    }.getOrDefault(emptyList())
                }
            }.awaitAll().flatten().distinctBy { it.id }
        }
        SourceSnapshot(id, displayName, categories, items)
    }

    companion object {
        val CATEGORIES = listOf(
            "movie" to "电影", "tv" to "电视剧", "anime" to "动漫",
            "variety" to "综艺", "short" to "短剧",
        )

        object VodRoutes {
            fun home(category: String, refresh: Boolean = false) =
                "/vod/home?category=$category" + if (refresh) "&refresh=1" else ""
            fun filters(category: String) = "/vod/filters/$category"
            /**
             * [deferSources] 只取豆瓣元数据（简介/评分/年份地区类型），跳过全量片源解析。
             * hero 简介只要元数据：带上它会等后端把 22 个站点的搜索+探测全跑完（实测桌面
             * 7.5s，电视弱网远超客户端 45s 读超时）→ 简介长时间停在「加载简介…」。
             */
            fun detail(id: String, deferSources: Boolean = false, refresh: Boolean = false): String {
                val deferQuery = if (deferSources) "?defer_sources=1" else ""
                val refreshQuery = if (!refresh) "" else if (deferSources) "&refresh=1" else "?refresh=1"
                return "/vod/detail/${id.removePrefix("douban:")}$deferQuery$refreshQuery"
            }

            /**
             * 云端 Resolver：按元数据中心永久 ID 取线路引用排序。
             * [episode] 是 1-based（与后端 `/vod/resolve` 的契约一致）。
             */
            fun resolve(mytvId: Long, episode: Int = 1): String =
                "/vod/resolve/$mytvId?episode=${episode.coerceAtLeast(1)}"
        }

        private fun fetchJson(base: String, path: String, client: OkHttpClient): JSONObject {
            val request = Request.Builder().url(base.trimEnd('/') + path)
                .header("User-Agent", "MyTV Android").build()
            client.newCall(request).execute().use { response ->
                require(response.isSuccessful) { "HTTP ${response.code} $path" }
                return JSONObject(response.body?.string().orEmpty())
            }
        }

        private fun strings(array: JSONArray?): List<String> =
            (0 until (array?.length() ?: 0)).mapNotNull { array?.optString(it)?.takeIf(String::isNotBlank) }

        /** 豆瓣图床（img1..img9.doubanio.com / asset）按 Referer 防盗链：App 直连回 HTTP 418，
         *  客户端解码失败表现为「海报整片不出图」。统一经后端 /hhkan/proxy 中继——后端按豆瓣
         *  自己的 Referer 出站并在各图床前端之间轮换（见 proxy.py `_img_fetch_with_mirrors`）。 */
        private val DOUBAN_IMG_RE = Regex("""^https?://(?:img\d+\.doubanio\.com|asset\.doubanio\.com)/""", RegexOption.IGNORE_CASE)

        fun relayPoster(base: String, raw: String): String {
            val u = raw.trim()
            if (u.length < 10 || !u.startsWith("http")) return u
            return if (base.isBlank() || !DOUBAN_IMG_RE.containsMatchIn(u)) u
            else base.trimEnd('/') + "/hhkan/proxy?u=" + URLEncoder.encode(u, "UTF-8")
        }

        /** 豆瓣同一张剧照有多个尺寸前缀，列表接口只给最小号（s_ratio_poster ≈ 240×360）。
         *  影视页 hero 是全宽铺图，240 宽拉到 1920 会糊；后端 `/vod/home` 已下发 hero_url
         *  （/view/photo/large/public/，1142×1600），拿不到就在这里就地换算。 */
        private val DOUBAN_PHOTO_RE =
            Regex("""^(https?://[^/]*doubanio\.com)/view/photo/[^/]+/(?:public/)?(p\d+\.[A-Za-z]+)$""")

        fun heroPoster(raw: String): String {
            val u = raw.trim()
            val m = DOUBAN_PHOTO_RE.find(u) ?: return u
            return "${m.groupValues[1]}/view/photo/large/public/${m.groupValues[2]}"
        }

        fun parseItems(array: JSONArray?, category: String, base: String = ""): List<VodItem> =
            (0 until (array?.length() ?: 0)).mapNotNull { index ->
                val item = array?.optJSONObject(index) ?: return@mapNotNull null
                val rawId = item.optString("id").removePrefix("douban:")
                val title = item.optString("title").trim()
                if (rawId.isBlank() || title.isBlank()) return@mapNotNull null
                val rawPoster = item.optString("poster_url")
                VodItem(
                    id = "douban:$rawId", sourceId = "douban", title = title,
                    categoryId = "douban:$category", year = item.optString("year"),
                    area = strings(item.optJSONArray("regions")).joinToString(" / "),
                    rating = item.optDouble("rating", 0.0), desc = item.optString("summary"),
                    posterUrl = relayPoster(base, rawPoster),
                    heroUrl = relayPoster(base, heroPoster(item.optString("hero_url").ifBlank { rawPoster })),
                    tags = strings(item.optJSONArray("genres")),
                    detailRef = rawId,
                    mytvId = item.optString("mytv_id").takeIf(String::isNotBlank).orEmpty(),
                )
            }

        /** Convert a local metadata row while keeping the existing Douban key for user records. */
        fun fromCatalogMedia(media: CatalogMedia, imageProxyBase: String = ""): VodItem {
            val legacy = media.legacyRef
            val id = legacy ?: "mytv:${media.mytvId}"
            val mediaType = media.mediaType.trim().lowercase()
            val category = when {
                mediaType in setOf("tv", "series", "tv_series", "show") -> "tv"
                mediaType in setOf("anime", "animation") -> "anime"
                mediaType in setOf("variety", "short") -> mediaType
                else -> "movie"
            }
            val poster = relayPoster(imageProxyBase, media.posterUrl)
            val meta = listOf(media.year, media.regions.joinToString(" / "), media.genres.joinToString(" / "))
                .filter(String::isNotBlank).joinToString(" / ")
            return VodItem(
                id = id,
                sourceId = if (legacy == null) "metadata" else "douban",
                title = media.title,
                categoryId = "douban:$category",
                year = media.year,
                area = media.regions.joinToString(" / "),
                rating = media.rating,
                desc = media.overview,
                posterUrl = poster,
                heroUrl = relayPoster(imageProxyBase, heroPoster(media.posterUrl)),
                tags = media.genres,
                detailRef = legacy?.removePrefix("douban:").orEmpty(),
                meta = meta,
                actors = media.actors.joinToString(" / "),
                mytvId = media.mytvId,
                searchTerms = media.searchTerms,
            )
        }

        fun fetchHome(
            base: String,
            client: OkHttpClient,
            category: String,
            refresh: Boolean = false,
        ): HhkanSource.HomeData {
            val json = fetchJson(base, VodRoutes.home(category, refresh), client)
            val sections = json.optJSONArray("sections")
            return HhkanSource.HomeData(
                sections = (0 until (sections?.length() ?: 0)).mapNotNull { index ->
                    val section = sections?.optJSONObject(index) ?: return@mapNotNull null
                    HhkanSource.ChannelSection(section.optString("title"), parseItems(section.optJSONArray("items"), category, base))
                }, carousel = emptyList(),
                stale = json.optBoolean("stale", false),
                source = json.optString("source"),
            )
        }

        fun fetchFilters(base: String, client: OkHttpClient, category: String): HhkanSource.FilterOptions {
            val json = fetchJson(base, VodRoutes.filters(category), client)
            return HhkanSource.FilterOptions(
                types = strings(json.optJSONArray("types")), areas = strings(json.optJSONArray("areas")),
                langs = emptyList(), years = strings(json.optJSONArray("years")),
                ratings = strings(json.optJSONArray("ratings")),
            )
        }

        fun fetchShow(base: String, client: OkHttpClient, category: String, type: String = "", area: String = "", year: String = "", rating: String = "", sort: String = "hot", page: Int = 1): HhkanSource.ShowPage {
            val query = listOf("genre" to type, "region" to area, "year" to year, "rating" to rating, "sort" to sort, "page" to page.toString())
                .joinToString("&") { (key, value) -> "$key=${URLEncoder.encode(value, "UTF-8")}" }
            val json = fetchJson(base, "/vod/show/$category?$query", client)
            return HhkanSource.ShowPage(
                items = parseItems(json.optJSONArray("items"), category, base),
                hasMore = json.optBoolean("has_more"),
                upstreamAvailable = json.optBoolean("upstream_available", true),
            )
        }

        fun fetchDetail(
            base: String,
            client: OkHttpClient,
            id: String,
            deferSources: Boolean = false,
            refresh: Boolean = false,
        ): HhkanSource.Companion.DetailInfo {
            if (id.startsWith("hhkan:")) {
                return HhkanSource.fetchDetail(
                    base = base,
                    okHttp = client,
                    vid = id.removePrefix("hhkan:"),
                    enrich = refresh && !deferSources,
                )
            }
            val json = fetchJson(base, VodRoutes.detail(id, deferSources, refresh), client)
            val lineArray = json.optJSONArray("sources")
            val lines = (0 until (lineArray?.length() ?: 0)).mapNotNull lineMap@ { lineIndex ->
                val line = lineArray?.optJSONObject(lineIndex) ?: return@lineMap null
                val lineId = line.optString("id")
                val episodeArray = line.optJSONArray("episodes")
                val episodes = (0 until (episodeArray?.length() ?: 0)).mapNotNull episodeMap@ { episodeIndex ->
                    val episode = episodeArray?.optJSONObject(episodeIndex) ?: return@episodeMap null
                    Triple(episode.optString("name", "第${episodeIndex + 1}集"), lineId, episodeIndex.toString())
                }
                HhkanSource.Companion.LineDetail(line.optString("name", "线路${lineIndex + 1}"), episodes)
            }
            return HhkanSource.Companion.DetailInfo(
                title = json.optString("title"), year = json.optString("year"),
                desc = json.optString("summary"), poster = relayPoster(base, json.optString("poster_url")),
                rating = json.optDouble("rating", 0.0),
                meta = listOf(json.optString("year"), strings(json.optJSONArray("regions")).joinToString(" / "), strings(json.optJSONArray("genres")).joinToString(" / ")).filter(String::isNotBlank).joinToString(" / "),
                actors = strings(json.optJSONArray("actors")).joinToString(" / "),
                director = strings(json.optJSONArray("directors")).joinToString(" / "), lines = lines,
            )
        }

        fun fetchPlay(base: String, client: OkHttpClient, id: String, lineId: String, episode: String): List<String> {
            if (id.startsWith("hhkan:")) {
                return HhkanSource.fetchPlay(
                    base = base,
                    okHttp = client,
                    vodId = id.removePrefix("hhkan:"),
                    pid = lineId,
                    vid = episode,
                ).takeIf(String::isNotBlank)?.let(::listOf).orEmpty()
            }
            val path = "/vod/play/${id.removePrefix("douban:")}/${URLEncoder.encode(lineId, "UTF-8")}/${episode.toIntOrNull() ?: 0}"
            val json = fetchJson(base, path, client)
            return listOfNotNull(json.optString("url").takeIf(String::isNotBlank))
        }

        /**
         * 拉云端 Resolver 结果。**只做一次轻量请求**，任何失败都交由上层回落 `/vod/detail`——
         * 这里不抛异常之外的语义：解析失败返回 null，由 [SourceResolveResult.usable] 决定回落。
         */
        fun fetchResolve(
            base: String,
            client: OkHttpClient,
            mytvId: Long,
            episode: Int = 1,
        ): SourceResolveResult? {
            if (mytvId <= 0L) return null
            val json = runCatching {
                fetchJson(base, VodRoutes.resolve(mytvId, episode), client)
            }.getOrNull() ?: return null
            return parseResolve(json, mytvId, episode)
        }

        private fun parseCandidate(obj: JSONObject?, fallbackEpisodeIndex: Int): ResolvedSource? {
            obj ?: return null
            val sourceId = obj.optString("source_id").trim()
            val contentId = obj.optString("content_id").trim()
            if (sourceId.isBlank() || contentId.isBlank()) return null
            return ResolvedSource(
                sourceId = sourceId,
                contentId = contentId,
                sourceName = obj.optString("source_name").trim(),
                episodeIndex = obj.optInt("episode_index", fallbackEpisodeIndex),
                matchScore = obj.optDouble("match_score", 0.0),
            )
        }

        fun parseResolve(
            json: JSONObject,
            mytvId: Long,
            episode: Int,
        ): SourceResolveResult {
            // 候选缺省回落到请求的集号（后端 episode 是 1-based，内部 0-based），避免 0 被当成第 0 集。
            val fallbackIndex = episode - 1
            val backupsArray = json.optJSONArray("backups")
            val backups = (0 until (backupsArray?.length() ?: 0)).mapNotNull { index ->
                parseCandidate(backupsArray?.optJSONObject(index), fallbackIndex)
            }
            val cache = json.optJSONObject("cache")
            return SourceResolveResult(
                mytvId = json.optLong("mytv_id", mytvId),
                episode = json.optInt("episode", episode),
                status = json.optString("status").ifBlank { "TEMPORARILY_UNAVAILABLE" },
                preferred = parseCandidate(json.optJSONObject("preferred"), fallbackIndex),
                backups = backups,
                cacheKind = cache?.optString("kind").orEmpty(),
                cacheAgeMs = cache?.optLong("age_ms", 0L) ?: 0L,
            )
        }
    }
}

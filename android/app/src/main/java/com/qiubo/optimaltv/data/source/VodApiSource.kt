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
             * 云端 Source Resolver（2026-09-28）。返回线路**引用**（source_id/content_id），
             * 不含播放地址——播放地址仍由 [/vod/play] 在起播那一刻解析，避免把带分钟级
             * 签名的直链提前落进缓存。
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
            val path = "/vod/play/${id.removePrefix("douban:")}/${URLEncoder.encode(lineId, "UTF-8")}/${episode.toIntOrNull() ?: 0}"
            val json = fetchJson(base, path, client)
            return listOfNotNull(json.optString("url").takeIf(String::isNotBlank))
        }

        // ---------- 云端 Source Resolver（2026-09-28） ----------

        /**
         * 解析器返回的一条线路引用。字段与后端 `ResolveCandidate.to_public_dict()` 一一对应。
         *
         * 这里**没有 url 字段**：resolver 只给稳定标识，播放地址由 [fetchPlay] 在起播那一刻
         * 解析，避免把带分钟级签名的直链提前落进缓存。
         */
        data class ResolveCandidate(
            val sourceId: String,
            val sourceName: String,
            val contentId: String,
            val episodeIndex: Int,
            val matchScore: Int,
        ) {
            /**
             * 拼出 `/vod/play` 用的 line id。
             *
             * 后端 `vod_sources.py` 的线路 id 格式是 `"{source_id}:{content_id}:{lineIndex}"`
             *（`"%s:%s:%d" % (self.source.id, candidate.get("id"), index)`）。resolver 只回
             * source_id + content_id，第三段用 0 —— `/vod/play` 按整串 id 查线路行，
             * 取 0 命中该内容在源下的第一组线路。
             */
            fun lineId(): String = "$sourceId:$contentId:0"
        }

        /** 解析器整体结果。[status] 取值见 [RESOLVE_STATUSES]。 */
        data class ResolveResponse(
            val mytvId: Long,
            val episode: Int,
            val status: String,
            val preferred: ResolveCandidate?,
            val backups: List<ResolveCandidate>,
        ) {
            /**
             * 只有真正拿到线路引用才算「可用」。
             *
             * RESOLVING / FALLBACK_SEARCHING 是「服务端还在算」——此时没有 preferred，
             * 客户端必须回落到本地 `/vod/detail` 全量匹配，不能空着干等。
             * STALE_AVAILABLE 拿到的是过期索引里的线路，能播，只是可能不是最新集；
             * 允许使用（总比让用户看到空片源强）。
             */
            val isUsable: Boolean get() = preferred != null

            /** 服务端没给出线路引用时为 true —— 该状态下必须回落本地匹配。 */
            val needsFallback: Boolean get() = !isUsable
        }

        /** 后端 `vod_resolver.RESOLVE_STATUSES` 的全集。 */
        val RESOLVE_STATUSES = setOf(
            "RESOLVING", "FAST_PATH_FAILED", "FALLBACK_SEARCHING",
            "STALE_AVAILABLE", "TEMPORARILY_UNAVAILABLE", "MATCHED",
        )

        /**
         * 解析 `/vod/resolve` 响应。
         *
         * 严格容错：任何字段缺失/类型不符都退化成「没有线路」，由上层回落本地匹配。
         * 宁可少走一步云端，也不能因为一条脏数据把播放功能整个卡死。
         */
        fun parseResolve(json: JSONObject): ResolveResponse {
            val status = json.optString("status").trim().uppercase()
            fun candidate(raw: JSONObject?): ResolveCandidate? {
                if (raw == null) return null
                val sourceId = raw.optString("source_id").trim()
                val contentId = raw.optString("content_id").trim()
                if (sourceId.isBlank() || contentId.isBlank()) return null
                return ResolveCandidate(
                    sourceId = sourceId,
                    sourceName = raw.optString("source_name").trim(),
                    contentId = contentId,
                    episodeIndex = raw.optInt("episode_index", 0),
                    matchScore = raw.optInt("match_score", 0),
                )
            }
            val backupArray = json.optJSONArray("backups")
            val parsedBackups = (0 until (backupArray?.length() ?: 0)).mapNotNull { i ->
                candidate(backupArray?.optJSONObject(i))
            }
            val preferred = candidate(json.optJSONObject("preferred"))
            return ResolveResponse(
                mytvId = json.optLong("mytv_id", 0L),
                episode = json.optInt("episode", 1),
                status = status,
                // backups[0] 在「preferred 为空但 backups 非空」的异常形态下也当首选用，
                // 免得白白浪费一次已经算好的匹配结果；此时 backups 不重复计入。
                preferred = preferred ?: parsedBackups.firstOrNull(),
                backups = if (preferred != null) parsedBackups else parsedBackups.drop(1),
            )
        }

        /** 调 `/vod/resolve`。失败一律返回 null（上层回落本地匹配），不抛。 */
        fun fetchResolve(
            base: String,
            client: OkHttpClient,
            mytvId: Long,
            episode: Int = 1,
        ): ResolveResponse? = runCatching {
            parseResolve(fetchJson(base, VodRoutes.resolve(mytvId, episode), client))
        }.onFailure {
            android.util.Log.w("OTV", "resolve mytv=$mytvId ep=$episode 失败: ${it.message}")
        }.getOrNull()
    }
}

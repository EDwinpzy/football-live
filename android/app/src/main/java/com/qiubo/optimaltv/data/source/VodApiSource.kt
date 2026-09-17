package com.qiubo.optimaltv.data.source

import com.qiubo.optimaltv.data.model.Category
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

    override suspend fun snapshot(): SourceSnapshot = withContext(Dispatchers.IO) {
        val categories = CATEGORIES.map { Category("douban:${it.first}", it.second) }
        val items = coroutineScope {
            CATEGORIES.map { (key, _) ->
                async {
                    runCatching {
                        val json = fetchJson(baseUrl, "/vod/home?category=$key", okHttp)
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
            fun home(category: String) = "/vod/home?category=$category"
            fun filters(category: String) = "/vod/filters/$category"
            fun detail(id: String) = "/vod/detail/${id.removePrefix("douban:")}"
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
         *  （/view/photo/l/public/，1142×1600），拿不到就在这里就地换算。 */
        private val DOUBAN_PHOTO_RE =
            Regex("""^(https?://[^/]*doubanio\.com)/view/photo/[^/]+/(?:public/)?(p\d+\.[A-Za-z]+)$""")

        fun heroPoster(raw: String): String {
            val u = raw.trim()
            val m = DOUBAN_PHOTO_RE.find(u) ?: return u
            return "${m.groupValues[1]}/view/photo/l/public/${m.groupValues[2]}"
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
                )
            }

        fun fetchHome(base: String, client: OkHttpClient, category: String): HhkanSource.HomeData {
            val json = fetchJson(base, VodRoutes.home(category), client)
            val sections = json.optJSONArray("sections")
            return HhkanSource.HomeData(
                sections = (0 until (sections?.length() ?: 0)).mapNotNull { index ->
                    val section = sections?.optJSONObject(index) ?: return@mapNotNull null
                    HhkanSource.ChannelSection(section.optString("title"), parseItems(section.optJSONArray("items"), category, base))
                }, carousel = emptyList(),
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
            return HhkanSource.ShowPage(parseItems(json.optJSONArray("items"), category, base), json.optBoolean("has_more"))
        }

        fun fetchDetail(base: String, client: OkHttpClient, id: String): HhkanSource.Companion.DetailInfo {
            val json = fetchJson(base, VodRoutes.detail(id), client)
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
    }
}

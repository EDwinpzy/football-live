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
import org.json.JSONObject

/**
 * 原版后端影视聚合源（内容与原版 web 一致）：
 * 复用 web/backend/python/proxy.py 的 hhkan 系列 API，后端零改动。
 *
 * 目录懒解析策略（hhkan 直链必须逐集解析，无法静态枚举）：
 * - snapshot() 只拉各频道列表（元数据 + detailRef=hhkan:vid），episodes 留空；
 * - 详情时 fetchDetail() 解析多线路选集（原版同款：站内线路即「线路」概念）；
 * - 起播/换线时 fetchPlay() 把 {vodid}-{pid}-{vid} 解析成直链 m3u8。
 */
class HhkanSource(
    private val baseUrl: String,
    private val okHttp: OkHttpClient,
) : ContentSource {

    override val id = "hhkan"
    override val displayName = "影视聚合·原版后端"

    private var cache: Pair<Long, SourceSnapshot>? = null

    override suspend fun snapshot(): SourceSnapshot = withContext(Dispatchers.IO) {
        cache?.takeIf { System.currentTimeMillis() - it.first < 120_000 }?.second?.let { return@withContext it }
        val base = baseUrl.trimEnd('/')
        // 频道与原版 _HHKAN_CHANNELS 一致；并发拉取，单频道失败跳过
        val cats = CHANNELS.map { (cid, name) -> Category("hhkan:$cid", name) }
        val items = coroutineScope {
            CHANNELS.map { (cid, name) ->
                async {
                    runCatching {
                        val arr = fetchJson(base, "/hhkan/channel/$cid", okHttp).getJSONArray("items")
                        parseVodItems(arr, "hhkan:$cid", base)
                    }.onFailure { android.util.Log.w("OTV", "hhkan channel $cid 失败: ${it.message}") }
                        .getOrDefault(emptyList())
                }
            }.awaitAll().flatten()
        }
        val snap = SourceSnapshot(id, displayName, cats, items)
        cache = System.currentTimeMillis() to snap
        snap
    }

    // 数据类型声明为类体嵌套（companion 内嵌套类的 import 路径不合法：HhkanSource.ChannelSection）
    /** 频道页三栏目数据 */
    data class ChannelSection(val name: String, val items: List<VodItem>)

    /** /hhkan/home 轮播焦点图（源站首页 3360×1080 横版大图 + 标题 + 标签） */
    data class CarouselItem(val vid: String, val title: String, val backdrop: String, val tags: List<String>)

    /** GET /hhkan/home → 源站首页精选板块（~9 个）+ 轮播焦点图 */
    data class HomeData(
        val sections: List<ChannelSection>,
        val carousel: List<CarouselItem>,
        /** 豆瓣首页缓存过期时先返回快照；客户端据此等待后台刷新后的新结果。 */
        val stale: Boolean = false,
        val source: String = "",
    )

    /** GET /hhkan/filters/1 → 全站通用筛选项（类型/地区/语言/年份，全部影视页用） */
    data class FilterOptions(
        val types: List<String> = emptyList(),
        val areas: List<String> = emptyList(),
        val langs: List<String> = emptyList(),
        val years: List<String> = emptyList(),
        val ratings: List<String> = emptyList(),
    )

    /** GET /hhkan/show/{cid}?page&by&type&area&lang&year → 筛选分页列表 + 是否还有下一页 */
    /** `upstreamAvailable=false` = 豆瓣暂时不可用（被拦/超时），结果来自缓存或快照：
     *  界面必须说「数据可能不是最新」或「暂时获取不到」，不能显示成「没有符合条件的内容」。 */
    data class ShowPage(val items: List<VodItem>, val hasMore: Boolean, val upstreamAvailable: Boolean = true)

    companion object { // MARK-240826
        /** 与原版 proxy.py _HHKAN_CHANNELS 保持一致 */
        val CHANNELS = linkedMapOf(1 to "电影", 2 to "电视剧", 3 to "动漫", 4 to "综艺", 6 to "短剧")
        /** 分类页固定信息架构；源站标题可变，产品标题不可变。 */
        val LIB_BLOCK_ORDER = listOf("最近热门", "最新上线", "最近更新")
        private val SOURCE_SECTION_ORDER = listOf("最新上线", "最近热门", "最近更新")

        private fun canonicalChannelBlockName(raw: String): String? {
            val name = raw.replace(Regex("\\s+"), "")
            return when {
                Regex("更新|连载|追更").containsMatchIn(name) -> "最近更新"
                Regex("热门|热播|推荐|人气|精选").containsMatchIn(name) -> "最近热门"
                Regex("最新|上线|上新|新片|新剧").containsMatchIn(name) -> "最新上线"
                else -> null
            }
        }

        /**
         * 将好好看可能变动的栏目文案归一为产品的三个固定栏目。
         * 源站仅改标题、布局仍为三段时，未识别段按频道原有顺序补位，避免整组消失。
         */
        fun normalizeChannelSections(
            sections: List<ChannelSection>,
            includeEmpty: Boolean = false,
        ): List<ChannelSection> {
            val picked = linkedMapOf<String, ChannelSection>()
            val unknown = mutableListOf<ChannelSection>()
            sections.forEach { section ->
                if (section.items.isEmpty()) return@forEach
                val target = canonicalChannelBlockName(section.name)
                if (target == null) {
                    unknown += section
                } else {
                    val old = picked[target]
                    if (old == null || section.items.size > old.items.size) {
                        picked[target] = ChannelSection(target, section.items)
                    }
                }
            }
            SOURCE_SECTION_ORDER.forEach { target ->
                if (target !in picked && unknown.isNotEmpty()) {
                    picked[target] = ChannelSection(target, unknown.removeAt(0).items)
                }
            }
            return LIB_BLOCK_ORDER.mapNotNull { name ->
                picked[name] ?: if (includeEmpty) ChannelSection(name, emptyList()) else null
            }
        }

        private fun fetchJson(base: String, path: String, okHttp: OkHttpClient): JSONObject {
            val req = Request.Builder()
                .url(base.trimEnd('/') + path)
                .header("User-Agent", "Mozilla/5.0 (Linux; Android 11) OptimalTV/1.0")
                .build()
            okHttp.newCall(req).execute().use { resp ->
                require(resp.isSuccessful) { "HTTP ${resp.code} $path" }
                return JSONObject(resp.body?.string().orEmpty())
            }
        }

        /** 详情解析结果：线路列表（每条带集数引用 pid/vid）+ 元数据补全 */
        data class LineDetail(val name: String, val episodes: List<Triple<String, String, String>>) // (集名, pid, vid)

        data class DetailInfo(
            val title: String,
            val year: String,
            val desc: String,
            val poster: String,
            val rating: Double,
            val meta: String = "",
            /** 演员表（已清洗分隔符与换行空白，join " / "） */
            val actors: String = "",
            val director: String = "",
            val lines: List<LineDetail>,
        )

        /** 源站图片 CDN 域（vres.* 等）——模拟器/无代理网络直连不可达，统一经后端 /hhkan/proxy 中继 */
        private val CDN_HOST_RE = Regex("""^https?://(vres\.[^/]+|img\.hhkan[^/]*|[^/]*\.hhkan[^/]*)/""", RegexOption.IGNORE_CASE)

        fun relayed(base: String, raw: String): String {
            val u = raw.trim()
            if (u.length < 10 || !u.startsWith("http")) return u
            return if (CDN_HOST_RE.containsMatchIn(u)) {
                base.trimEnd('/') + "/hhkan/proxy?u=" + java.net.URLEncoder.encode(u, "UTF-8")
            } else u
        }

        /** 接口 items[] 条目 → VodItem（id/title/cover/score/remark 契约见测试文档 §接口） */
        private fun parseVodItems(arr: org.json.JSONArray?, categoryId: String, base: String = ""): List<VodItem> =
            (0 until (arr?.length() ?: 0)).mapNotNull { i ->
                val o = arr?.optJSONObject(i) ?: return@mapNotNull null
                val vid = o.optLong("id", 0L)
                if (vid <= 0L) return@mapNotNull null
                VodItem(
                    id = "hhkan:$vid",
                    sourceId = "hhkan",
                    title = o.optString("title").trim(),
                    categoryId = categoryId,
                    rating = o.optDouble("score", 0.0),
                    posterUrl = if (base.isBlank()) o.optString("cover") else relayed(base, o.optString("cover")),
                    remark = com.qiubo.optimaltv.data.model.normalizeRemark(o.optString("remark")),
                    detailRef = "hhkan:$vid",
                ).takeIf { it.title.isNotBlank() }
            }

        /** 页面级实时抓取（后端现场抓源站，冷抓 20s+）共用长读超时 client */
        private fun slow(okHttp: OkHttpClient): OkHttpClient =
            okHttp.newBuilder().readTimeout(45, java.util.concurrent.TimeUnit.SECONDS).build()

        /** GET /hhkan/home（后端实时抓源站首页，冷抓 20s+，须用长读超时 client；失败保留空态等待重试） */
        fun fetchHome(base: String, okHttp: OkHttpClient): HomeData {
            val o = fetchJson(base, "/hhkan/home", slow(okHttp))
            if (!o.optBoolean("ok", true) && o.optJSONArray("sections") == null) return HomeData(emptyList(), emptyList())
            val secArr = o.optJSONArray("sections")
            val sections = (0 until (secArr?.length() ?: 0)).mapNotNull { i ->
                val s = secArr?.optJSONObject(i) ?: return@mapNotNull null
                val items = parseVodItems(s.optJSONArray("items"), "", base)
                if (items.isEmpty()) null else ChannelSection(s.optString("title"), items)
            }
            val carArr = o.optJSONArray("carousel")
            val carousel = (0 until (carArr?.length() ?: 0)).mapNotNull { i ->
                val c = carArr?.optJSONObject(i) ?: return@mapNotNull null
                val vid = c.optLong("id", 0L)
                val tagsArr = c.optJSONArray("tags")
                CarouselItem(
                    vid = vid.toString(),
                    title = c.optString("title").trim(),
                    backdrop = relayed(base, c.optString("backdrop")),
                    tags = (0 until (tagsArr?.length() ?: 0)).mapNotNull { j -> tagsArr?.optString(j)?.takeIf { it.isNotBlank() } },
                ).takeIf { vid > 0 && it.backdrop.isNotBlank() }
            }
            return HomeData(sections, carousel)
        }

        /** GET /hhkan/channel/{cid} → 该频道最新上线列表（片库「最新上线」块数据源） */
        fun fetchChannelItems(base: String, okHttp: OkHttpClient, cid: Int): List<VodItem> =
            parseVodItems(fetchJson(base, "/hhkan/channel/$cid", slow(okHttp)).optJSONArray("items"), "hhkan:$cid", base)

        /** GET /hhkan/latest?page=1 → 全站最近更新（片库「最近更新」块数据源，与频道无关） */
        fun fetchLatest(base: String, okHttp: OkHttpClient): List<VodItem> =
            parseVodItems(fetchJson(base, "/hhkan/latest?page=1", slow(okHttp)).optJSONArray("items"), "", base)

        fun fetchFilters(base: String, okHttp: OkHttpClient, cid: Int = 1): FilterOptions {
            // filters/show 都是页面级实时抓取（后端现场抓源站，冷抓 20s+），用长读超时 client，
            // 默认 10s 超时会把「慢而能成」的请求截成失败（需求 影视#8 筛选结果不全的成因之一）
            val o = fetchJson(base, "/hhkan/filters/$cid", slow(okHttp))
            fun arr(name: String): List<String> {
                val a = o.optJSONArray(name) ?: return emptyList()
                return (0 until a.length()).mapNotNull { i -> a.optString(i).takeIf { it.isNotBlank() } }
            }
            return FilterOptions(arr("types"), arr("areas"), arr("langs"), arr("years"))
        }

        fun fetchShow(
            base: String,
            okHttp: OkHttpClient,
            cid: Int,
            type: String = "",
            area: String = "",
            lang: String = "",
            year: String = "",
            by: String = "3",
            page: Int = 1,
        ): ShowPage {
            val q = "page=$page&by=${java.net.URLEncoder.encode(by, "UTF-8")}" +
                "&type=${java.net.URLEncoder.encode(type, "UTF-8")}" +
                "&area=${java.net.URLEncoder.encode(area, "UTF-8")}" +
                "&lang=${java.net.URLEncoder.encode(lang, "UTF-8")}" +
                "&year=${java.net.URLEncoder.encode(year, "UTF-8")}"
            val o = fetchJson(base, "/hhkan/show/$cid?$q", slow(okHttp))
            return ShowPage(
                items = parseVodItems(o.optJSONArray("items"), "hhkan:$cid", base),
                hasMore = o.optBoolean("has_more", false),
            )
        }

        /** GET /hhkan/channel-sections/{cid} → 当前分类的固定三栏目（好好看主源）。 */
        fun fetchChannelSections(base: String, okHttp: OkHttpClient, cid: Int): List<ChannelSection> {
            val o = fetchJson(base, "/hhkan/channel-sections/$cid", slow(okHttp))
            val arr = o.optJSONArray("sections") ?: return normalizeChannelSections(emptyList(), includeEmpty = true)
            val raw = (0 until arr.length()).mapNotNull { i ->
                val s = arr.optJSONObject(i) ?: return@mapNotNull null
                val items = parseVodItems(s.optJSONArray("items"), "hhkan:$cid", base)
                ChannelSection(s.optString("name").ifBlank { s.optString("title") }, items)
            }
            return normalizeChannelSections(raw, includeEmpty = true)
        }

        /**
         * GET /hhkan/detail/{vid} → 多线路选集（4K 死线路已由后端过滤）。
         * [enrich] 仅在播放器起播前使用：详情页保持快速，播放器等到补充源后再自动选线。
         */
        fun fetchDetail(base: String, okHttp: OkHttpClient, vid: String, enrich: Boolean = false): DetailInfo {
            val path = "/hhkan/detail/$vid" + if (enrich) "?enrich=1" else ""
            val o = fetchJson(base, path, slow(okHttp))
            val lines = mutableListOf<LineDetail>()
            val srcArr = o.optJSONArray("sources")
            if (srcArr != null) {
                for (i in 0 until srcArr.length()) {
                    val s = srcArr.optJSONObject(i) ?: continue
                    val eps = mutableListOf<Triple<String, String, String>>()
                    val epArr = s.optJSONArray("episodes")
                    if (epArr != null) {
                        for (j in 0 until epArr.length()) {
                            val e = epArr.optJSONObject(j) ?: continue
                            eps += Triple(
                                e.optString("ep"),
                                e.optInt("pid").toString(),
                                e.optInt("vid").toString(),
                            )
                        }
                    }
                    if (eps.isNotEmpty()) lines += LineDetail(s.optString("name"), eps)
                }
            }
            return DetailInfo(
                title = o.optString("title"),
                year = o.optString("year"),
                desc = o.optString("desc"),
                poster = relayed(base, o.optString("cover")),
                rating = o.optDouble("score", 0.0),
                meta = o.optString("meta"),
                // 源站 actors 是「A\n / \nB」式脏串：按 / 拆分、压空白、去空段
                actors = o.optString("actors").split("/")
                    .map { it.replace(Regex("\\s+"), "").trim() }
                    .filter { it.isNotBlank() }
                    .joinToString(" / "),
                director = o.optString("director").replace(Regex("\\s+"), ""),
                lines = lines,
            )
        }

        /** GET /hhkan/play/{vodid}/{pid}/{vid}（三段斜杠）→ 该集该线路直链（取第一个可用的） */
        fun fetchPlay(base: String, okHttp: OkHttpClient, vodId: String, pid: String, vid: String): String {
            val o = fetchJson(base, "/hhkan/play/$vodId/$pid/$vid", slow(okHttp))
            val arr = o.optJSONArray("sources") ?: return ""
            for (i in 0 until arr.length()) {
                val u = arr.optJSONObject(i)?.optString("url").orEmpty()
                if (u.isNotBlank()) return u
            }
            return ""
        }
    }
}

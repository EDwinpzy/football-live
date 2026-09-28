package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.BuildConfig
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.analytics.PlaybackTelemetry
import com.qiubo.optimaltv.data.catalog.MetadataCatalogStore
import com.qiubo.optimaltv.data.db.LastGoodSourceEntity
import com.qiubo.optimaltv.data.model.Category
import com.qiubo.optimaltv.data.model.CatalogMedia
import com.qiubo.optimaltv.data.model.Episode
import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.data.source.ContentSource
import com.qiubo.optimaltv.data.source.HhkanSource
import com.qiubo.optimaltv.data.source.ResolverRanking
import com.qiubo.optimaltv.data.source.SourceResolveResult
import com.qiubo.optimaltv.data.source.VodApiSource
import com.qiubo.optimaltv.data.source.HhkanSource.ChannelSection as HSec
import com.qiubo.optimaltv.data.source.HhkanSource.Companion.DetailInfo
import com.qiubo.optimaltv.data.source.SourceSnapshot
import com.qiubo.optimaltv.data.source.SourceState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout

data class Aggregated(
    val categories: List<Category>,
    /** 浏览/搜索用去重列表（同 normTitle 只保留第一个源） */
    val dedupedItems: List<VodItem>,
    /** 全量条目按 normTitle 分组 → 详情页「线路」= 组内成员（跨源换源基础） */
    val groups: Map<String, List<VodItem>>,
)

data class CatalogState(
    val loading: Boolean = false,
    val catalog: Aggregated? = null,
    val sourceStates: List<SourceState> = emptyList(),
)

/** hero 条目（三来源归一：carousel 横版大图 / 首页热门板块竖版封面 / 目录竖版封面） */
data class HeroRef(val id: String, val title: String, val bg: String, val tag: String)

/**
 * 原版 refreshVod hero 降级链：carousel>=3 → 首页热门板块合并(hotHeroItems) → 目录前 6。
 *
 * 2026-09-17：口径从 HomeScreen.kt 提到这里，供 UI 渲染与启动门闸 hero 简介预载共用。
 * 旧实现两边各写一套（预载按 carousel 取 id、UI 实际按热门板块取），豆瓣源下
 * carousel 恒空 → 预载全是空转，hero 简介仍要等进页才发请求（用户反馈加载慢的成因）。
 */
fun heroRefs(catalog: Aggregated?, home: VodRepository.HomeState): List<HeroRef> {
    if (home.carousel.size >= 3) {
        return home.carousel.map { HeroRef("hhkan:" + it.vid, it.title, it.backdrop, it.tags.firstOrNull().orEmpty()) }
    }
    /* 2026-09-18：设备端豆瓣模块 /vod/home 的板块名就是「最近热门 / 最新上映 / 豆瓣高分」
       （douban_catalog.HOME_SECTION_NAMES），旧键表只有好好看时代的「近期热门…」，
       豆瓣源下恒不命中 → hero 只能靠目录兜底；目录一空 hero 就整条空掉。两套键都留下。 */
    val hotKeys = listOf("近期热门", "最近更新", "近期热播", "热播", "近期热门电影", "近期热门剧集",
        "最近热门", "最新上映", "豆瓣高分")
    val hot = home.sections
        .filter { sec -> hotKeys.any { sec.name.contains(it) } }
        // hero 用大图：列表接口给的最小号铺满整屏会糊（见 VodItem.heroUrl）
        .flatMap { sec -> sec.items.map { HeroRef(it.id, it.title, it.heroUrl.ifBlank { it.posterUrl }, sec.name) } }
        .distinctBy { it.id }
    if (hot.isNotEmpty()) return hot.take(8)
    return catalog?.dedupedItems?.filter { it.posterUrl.isNotBlank() }?.take(6)
        ?.map { HeroRef(it.id, it.title, it.heroUrl.ifBlank { it.posterUrl }, "") }.orEmpty()
}

/** 豆瓣刮削结果（预探测替换：字段空则不替换） */
data class DoubanInfo(val found: Boolean, val rating: String, val desc: String, val poster: String)

private class SourceResult(val source: ContentSource, val snapshot: SourceSnapshot?, val error: Exception?)

/**
 * 目录来源硬边界：浏览卡片只能由好好看产生。
 *
 * 旧版本曾把补充播放源写入同一个目录快照；应用升级后即使网络层已经改为好好看
 * 单源，旧快照仍会先上屏。把策略集中在这里，避免恢复、写入两条路径再次漂移。
 */
internal object CatalogOriginPolicy {
    const val SNAPSHOT_VERSION = 4

    /**
     * 内置 assets/catalog_seed.json 可接受的版本集合（v3 = 随包发布的目录种子，v4 = 当前快照格式）。
     *
     * 放宽只针对 seed：它是随 APK 发布的静态资源，由真实后端导出、CI 不可重新生成，体积固定约 100KB，
     * 不会像用户设备上的持久化快照那样随目录增长绕过 MAX_ITEMS / MAX_BYTES 上限。
     * 持久化快照仍走 [SNAPSHOT_VERSION] 严格门槛，避免旧快照里的超大 items 绕过大小限制。
     */
    val SEED_ACCEPTED_VERSIONS = setOf(3, 4)

    /** 用户设备上持久化的目录快照：只接受当前版本。 */
    val PERSISTED_ACCEPTED_VERSIONS = setOf(SNAPSHOT_VERSION)

    fun allows(sourceId: String, itemId: String): Boolean =
        (sourceId == "douban" && itemId.startsWith("douban:")) ||
            (sourceId == "metadata" && itemId.startsWith("mytv:"))
}

internal object CatalogSnapshotPolicy {
    const val MAX_BYTES = 8L * 1024 * 1024
    const val MAX_ITEMS = 120

    fun isReadableSize(size: Long): Boolean = size in 1..MAX_BYTES
    fun shouldReadLegacy(metadataInstalled: Boolean, size: Long): Boolean =
        !metadataInstalled && isReadableSize(size)

    fun <T> boundedItems(items: List<T>): List<T> = items.take(MAX_ITEMS)
}

/** 随 APK 发布的内置目录种子文件名；与用户设备上持久化的磁盘快照区分开版本策略。 */
internal object CatalogSeedAsset {
    const val NAME = "catalog_seed.json"
}

internal object VodIdPolicy {
    fun isCatalogId(id: String): Boolean = id.startsWith("douban:") || id.startsWith("mytv:")
}

internal object PlaybackLineSelector {
    fun nextAfterFailure(rankedIds: List<String>, activeId: String?, failed: Set<String>): String? {
        val start = rankedIds.indexOf(activeId).coerceAtLeast(-1) + 1
        return rankedIds.drop(start).firstOrNull { it !in failed }
    }
}

internal data class CatalogSnapshot(
    val categories: List<Category>,
    val items: List<VodItem>,
)

/**
 * 解析目录快照 JSON。
 *
 * [acceptedVersions] 决定接受哪些 `v`：持久化快照传 [CatalogOriginPolicy.PERSISTED_ACCEPTED_VERSIONS]（严格 v4），
 * 内置种子传 [CatalogOriginPolicy.SEED_ACCEPTED_VERSIONS]（v3/v4）。其余校验逻辑两种来源完全一致。
 */
internal fun decodeSnapshot(text: String, acceptedVersions: Set<Int>): CatalogSnapshot? {
    val o = org.json.JSONObject(text)
    if (o.optInt("v") !in acceptedVersions) return null
    val catArr = o.optJSONArray("categories") ?: return null
    val itemArr = o.optJSONArray("items") ?: return null
    val cats = (0 until catArr.length()).mapNotNull { i ->
        val c = catArr.optJSONObject(i) ?: return@mapNotNull null
        Category(c.optString("id"), c.optString("name")).takeIf { it.id.isNotBlank() }
    }
    val items = (0 until itemArr.length()).mapNotNull { i ->
        val e = itemArr.optJSONObject(i) ?: return@mapNotNull null
        VodItem(
            id = e.optString("id"), sourceId = e.optString("s"), title = e.optString("t"),
            categoryId = e.optString("c"), year = e.optString("y"), area = e.optString("ar"),
            rating = e.optDouble("r", 0.0), posterUrl = e.optString("p"), heroUrl = e.optString("h"),
            remark = e.optString("rm"), detailRef = e.optString("d"),
            mytvId = e.optString("mytv_id").ifBlank { e.optString("m") },
            searchTerms = (0 until (e.optJSONArray("q")?.length() ?: 0))
                .mapNotNull { e.optJSONArray("q")?.optString(it)?.takeIf(String::isNotBlank) },
        ).takeIf { it.id.isNotBlank() && it.title.isNotBlank() }
    }
    if (cats.isEmpty() || items.isEmpty()) return null
    if (items.any { !CatalogOriginPolicy.allows(it.sourceId, it.id) }) return null
    return CatalogSnapshot(cats, items)
}

/** 目录连续空结果时的后台重试间隔（毫秒）：内嵌后端冷启动/热更重启需要的时间窗。 */
private val CATALOG_RETRY_DELAYS = longArrayOf(5_000L, 10_000L, 20_000L, 30_000L)

/**
 * 取源阶梯的可断言契约。
 *
 * 抽成对象而不是散落的字面量，是为了让「本地优先、云端不拖起播」这条方案硬约束
 * 有一个**可执行**的落点：生产代码读这里，单测也读这里，改坏了会红。
 */
object VodRepositoryConfig {

    /**
     * 云端 Resolver 的**硬上限**（毫秒）。
     *
     * Resolver 是排序层，本机 detail 已经先拿到线了，它只是锦上添花。
     * 方案 P95 首帧 ≤ 4s，排序层多等一秒都是白等——所以给它 1.5s 封顶：
     * 命中就重排（省掉后续人工换线），不命中/超时/机房出口不通就按本地原序起播，
     * 观感与旧版完全一致但**不再被云端拖住**。
     */
    const val RESOLVER_TIMEOUT_MS = 1_500L

    /**
     * 阶梯的求值次序。`detail`（端侧多源匹配）是唯一能凭空产出可播线路的一层，
     * 必须最先跑；`cloud`（云端 Resolver）只是它之上的排序层，恒定排在最后。
     */
    val TIER_ORDER = listOf("detail", "last_good", "cloud")

    /**
     * 云端不可用时的收敛动作：**原样返回端侧已解析出的线路**。
     *
     * 明确写出来而不是让调用方自己判断，是为了让「云端失败不得改变用户拿到什么」
     * 变成一条可测语句——超时、不可达、无候选、状态非 MATCHED 全走这一个出口。
     */
    fun <T> onCloudUnavailable(local: List<T>): List<T> = local
}

/** 内容仓库（技术方案 §3.1 业务层·内容仓库）：好好看目录 / 分类 / 搜索。 */
class VodRepository(
    private val settings: SettingsStore,
    private val okHttp: okhttp3.OkHttpClient,
    private val appContext: android.content.Context,
) {
    private val metadataCatalog by lazy {
        MetadataCatalogStore(appContext, okHttp, BuildConfig.API_BASE_URL)
    }

    private val _state = MutableStateFlow(CatalogState(loading = true))
    val state: StateFlow<CatalogState> = _state.asStateFlow()

    /** 详情专用客户端：豆瓣详情要做「多线路测活 + 清晰度/延时综合排序」，冷路径实测
     *  15～20s（RemoteApiSource.defaultClient 的 10s 读超时会让它整条失败 → 详情页
     *  「暂无片源/暂无简介」）；这里放宽到 45s，与 HhkanSource 的详情口径一致。 */
    private val detailClient by lazy {
        okHttp.newBuilder().readTimeout(45, java.util.concurrent.TimeUnit.SECONDS).build()
    }

    /**
     * 云端 Resolver 专用客户端：**独立且超时不拖累其他请求**。
     *
     * 不能直接复用 [okHttp]（10s 读超时）：Resolver 不可达时会占住调用方 10s，
     * 而它只是排序层，等 10s 毫无收益。callTimeout 保证整趟（含连接/读/写）封顶，
     * 与外层 [RESOLVER_TIMEOUT_MS] 一起构成双保险——任一先到都立刻走本地原序。
     */
    private val resolveClient by lazy {
        okHttp.newBuilder()
            .callTimeout(
                VodRepositoryConfig.RESOLVER_TIMEOUT_MS,
                java.util.concurrent.TimeUnit.MILLISECONDS,
            )
            .build()
    }

    /** 内置后端就绪探测：仅当 base 指向 127.0.0.1/localhost（内置后端冷启动需几秒）时轮询。
     *  v1.23（2026-09-06 进入提速）：轮询挪 IO——okHttp execute() 是同步阻塞调用，
     *  boot() 在 Main dispatcher 直接调用本函数，主线程同步网络在部分 ROM/模拟器上
     *  被 BlockGuard 拒绝（异常被 runCatching 吞 → 30 轮全空转 = 稳定白等 15s，
     *  「进入播放页加载慢」的直接来源；后端已就绪时本应毫秒级通过）。 */
    private suspend fun waitBackendReady(base: String, tries: Int = 30) {
        if (!base.contains("127.0.0.1") && !base.contains("localhost")) return
        kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
            repeat(tries) {
                val ok = runCatching {
                    val req = okhttp3.Request.Builder().url(base.trimEnd('/') + "/api/health").build()
                    okHttp.newCall(req).execute().use { it.isSuccessful }
                }.getOrDefault(false)
                if (ok) return@withContext
                kotlinx.coroutines.delay(500)
            }
        }
    }

    /** 播放器秒进（debug 注入/恢复播放）时的后端就绪闸门：冷启动期详情/直链请求会
     *  连接拒绝被 runCatching 吞掉 → 假性「该集没有可用线路」（v1.14 实测复现）。 */
    suspend fun awaitBackendReady() {
        waitBackendReady(settings.current().hhkanBaseUrl)
    }

    /** 等待目录可用（内存目录或磁盘快照任一；refresh 仍在后台继续）——
     *  启动门闸深预载用：不等整轮 refresh 网络抓取，慢网不把门闸预算吃光。 */
    suspend fun awaitCatalogAvailable() {
        while (_state.value.catalog == null) kotlinx.coroutines.delay(150)
    }

    suspend fun refresh(force: Boolean = false) {
        _state.value = _state.value.copy(loading = true)
        val cfg = settings.current()
        try {
          metadataCatalog.refreshIfNeeded(force)
          val installedMetadata = metadataCatalog.hasInstalledDatabase()
          if (installedMetadata) {
              val items = metadataCatalog.previewRows().map { VodApiSource.fromCatalogMedia(it, cfg.hhkanBaseUrl) }
              val categories = metadataCategoriesForKeys(metadataCatalog.categoryKeys())
              val groups = items.groupBy { it.normTitle }
              _state.value = CatalogState(
                  loading = false,
                  catalog = Aggregated(categories, groups.map { it.value.first() }, groups),
                  sourceStates = listOf(SourceState("metadata", "本地影视目录", true, "${items.size} 部")),
              )
              catalogRetry = 0
              // SQLite is authoritative, including a valid empty result; keep a bounded preview in memory.
              return
          }

          // 无有效 metadata SQLite 时保留现有本地 API / JSON 种子回退链。
          // 内置后端就绪探测：app 内置 proxy 冷启动需几秒，先轮询 /api/health。
          waitBackendReady(cfg.hhkanBaseUrl, tries = 80)
          val sources = buildList {
              if (cfg.hhkanBaseUrl.isNotBlank()) add(VodApiSource(cfg.hhkanBaseUrl, okHttp))
          }
          coroutineScope {
            val results: List<SourceResult> = sources.map { s ->
                async {
                    try {
                        val snapshot = s.snapshot(refresh = force)
                        SourceResult(s, snapshot, null)
                    } catch (e: Exception) {
                        SourceResult(s, null, e)
                    }
                }
            }.awaitAll()

            val cats = linkedMapOf<String, Category>()
            val allItems = mutableListOf<VodItem>()
            val states = mutableListOf<SourceState>()
            for (r in results) {
                states += if (r.snapshot != null) {
                    r.snapshot.categories.forEach { c -> cats.putIfAbsent(c.id, c) }
                    allItems += r.snapshot.items
                    SourceState(r.source.id, r.source.displayName, true, "${r.snapshot.items.size} 部")
                } else {
                    SourceState(r.source.id, r.source.displayName, false, r.error?.message ?: "加载失败")
                }
            }
            val groups = allItems.groupBy { it.normTitle }
            /* 2026-09-18 用户报障「影视界面崩了」：空结果绝不覆盖已有目录。
               内嵌后端在热更解压 / 冷启动期（Chaquopy + 36MB 包解压 + 快照装载）可能
               十几秒还没就绪，首轮 refresh 的 5 路 /vod/home 会集体连接失败；旧实现
               仍然把目录替换成「有分类、0 条目」的空目录 → 影视页 hero 退化成空底板
               （OtvColors.Panel）、片库分类卡与三栏全空，且本次会话不再重试。
               现在：有旧目录就保留并后台退避重试；一次都没成功过才写入空目录。 */
            val previous = _state.value.catalog
            if (allItems.isEmpty() && previous != null && previous.dedupedItems.isNotEmpty()) {
                _state.value = CatalogState(loading = false, catalog = previous, sourceStates = states)
                scheduleCatalogRetry()
                return@coroutineScope
            }
            _state.value = CatalogState(
                loading = false,
                catalog = Aggregated(
                    categories = cats.values.toList(),
                    dedupedItems = groups.map { it.value.first() },
                    groups = groups,
                ),
                sourceStates = states,
            )
            if (allItems.isEmpty()) scheduleCatalogRetry() else catalogRetry = 0
            // v1.10 目录磁盘快照：任一真实源成功才落盘（演示兜底不落），下次启动秒开
            if (results.any { it.snapshot != null }) {
                val catsOut = cats.values.toList()
                val itemsOut = allItems.toList()
                repoScope.launch { runCatching { writeSnapshot(catsOut, itemsOut) } }
            }
          }
        } catch (e: Throwable) {
            /* 启动门闸超时会取消 refresh（withTimeoutOrNull），异常也会中断；这里保证
               loading 一定落回 false——否则 ensureCatalogFresh() 的 loading 守卫会永久
               挡住后续补抓，影视页再也回不来。 */
            _state.value = _state.value.copy(loading = false)
            throw e
        }
    }

    fun itemById(vodId: String): VodItem? =
        _state.value.catalog?.groups?.values?.flatten()?.firstOrNull { it.id == vodId }

    fun siblings(normTitle: String): List<VodItem> =
        _state.value.catalog?.groups?.get(normTitle) ?: emptyList()

    suspend fun search(query: String): List<VodItem> {
        val q = query.trim()
        if (q.isEmpty()) return emptyList()
        if (metadataCatalog.hasInstalledDatabase()) {
            return metadataCatalog.search(q)
                .map { VodApiSource.fromCatalogMedia(it, settings.current().hhkanBaseUrl) }
                .distinctBy(VodItem::normTitle)
        }
        return withContext(Dispatchers.Default) {
            _state.value.catalog?.dedupedItems?.filter { item ->
                item.title.contains(q, true) || item.tags.any { it.contains(q, true) }
                        || item.year == q || item.area.contains(q, true)
                        || item.searchTerms.any { it.contains(q, true) }
            } ?: emptyList()
        }
    }

    private fun metadataCategories(items: List<VodItem>): List<Category> = items
        .distinctBy { it.categoryId }
        .map { it.categoryId.removePrefix("douban:") }
        .let(::metadataCategoriesForKeys)

    private fun metadataCategoriesForKeys(keys: List<String>): List<Category> = keys.distinct().map { key ->
            Category("douban:$key", when (key) {
                "movie" -> "电影"
                "tv" -> "电视剧"
                "anime" -> "动漫"
                "variety" -> "综艺"
                "short" -> "短剧"
                else -> key
            })
        }

    private fun categoryForMediaType(mediaType: String): String = when (mediaType.trim().lowercase()) {
        "tv", "series", "tv_series", "show" -> "tv"
        "anime", "animation" -> "anime"
        "variety", "short" -> mediaType.trim().lowercase()
        else -> "movie"
    }

    /** 分类页三栏目缓存；好好看是唯一卡片目录源。 */
    private val _sections = MutableStateFlow<Map<Int, List<HSec>>>(emptyMap())
    val sectionsFlow: StateFlow<Map<Int, List<HSec>>> = _sections.asStateFlow()

    suspend fun channelSections(categoryId: String) {
        val key = categoryId.removePrefix("douban:")
        val cid = categoryNumber(key) ?: return
        if (_sections.value.containsKey(cid)) return
        val base = settings.current().hhkanBaseUrl
        val secs = runCatching { VodApiSource.fetchHome(base, okHttp, key).sections }.getOrDefault(emptyList())
        if (secs.any { it.items.isNotEmpty() }) _sections.value = _sections.value + (cid to secs)
    }

    /** 源站首页（原版 view-vod refreshVod：/hhkan/home sections + carousel） */

    data class HomeState(
        val loading: Boolean = false,
        val sections: List<HSec> = emptyList(),
        val carousel: List<HhkanSource.CarouselItem> = emptyList(),
    )

    private val _home = MutableStateFlow(HomeState())
    val homeFlow: StateFlow<HomeState> = _home.asStateFlow()

    /** repo 全局 scope：首页拉取不随 UI 组合取消（LaunchedEffect 被切页取消会导致 home 永远拿不到） */
    private val repoScope = kotlinx.coroutines.CoroutineScope(
        kotlinx.coroutines.SupervisorJob() + Dispatchers.IO,
    )

    /** 目录空结果的退避重试：连续为空时 5/10/20/30s 后台补抓（成功即清零）。
     *  2026-09-18：内嵌后端冷启动/热更重启期首轮抓取会全失败，旧实现没有重试，
     *  用户只有重启 app 才可能恢复。 */
    private var catalogRetry = 0
    private var catalogRetryJob: kotlinx.coroutines.Job? = null

    private var catalogRefreshJob: kotlinx.coroutines.Job? = null

    /** 影视页进入兜底：目录还是空的（或条目为空）就补一次刷新。 */
    fun ensureCatalogFresh(force: Boolean = false) {
        // 活跃 Job 判重对 force 也生效：影视页来回切会把 LaunchedEffect(Unit) 重新触发，
        // 无脑放行会让同一轮目录抓取并发叠好几份。
        if (catalogRefreshJob?.isActive == true) return
        val cat = _state.value.catalog
        if (!force && cat != null && cat.dedupedItems.isNotEmpty()) return
        if (_state.value.loading && !force) return
        catalogRefreshJob = repoScope.launch { runCatching { refresh(force = force) } }
    }

    private fun scheduleCatalogRetry() {
        if (catalogRetry >= CATALOG_RETRY_DELAYS.size) return
        if (catalogRetryJob?.isActive == true) return
        val wait = CATALOG_RETRY_DELAYS[catalogRetry]
        catalogRetry++
        android.util.Log.w("OTV", "目录为空，${wait}ms 后重试（第 $catalogRetry 次）")
        catalogRetryJob = repoScope.launch {
            kotlinx.coroutines.delay(wait)
            runCatching { refresh() }
                .onFailure { android.util.Log.w("OTV", "目录重试失败: ${it.message}") }
        }
    }

    private var homeJob: kotlinx.coroutines.Job? = null

    /**
     * 拉源站首页：精选板块 + 轮播焦点图。fire-and-forget（UI 触发后立即返回），
     * 守卫用活跃 Job 判重——不用 loading 布尔残留判重（协程取消后残留会永久挡住重试）。
     */
    fun refreshHome(force: Boolean = false) {
        // 进行中的 Job 一律复用（awaitHome 也靠它 join）；force 只绕过「已有内容不再抓」。
        if (homeJob?.isActive == true) return
        if (!force && (_home.value.sections.isNotEmpty() || _home.value.carousel.isNotEmpty())) return
        _home.value = _home.value.copy(loading = true)
        homeJob = repoScope.launch {
            val localItems = (
                metadataCatalog.topRows("movie", "hot") +
                    metadataCatalog.topRows("movie", "new") +
                    metadataCatalog.topRows("movie", "rating")
                ).distinctBy(CatalogMedia::mytvId)
                .map { VodApiSource.fromCatalogMedia(it, settings.current().hhkanBaseUrl) }
            if (localItems.isNotEmpty()) {
                val sections = localHomeSections(localItems)
                _home.value = HomeState(loading = false, sections = sections)
            }
            val base = settings.current().hhkanBaseUrl
            if (base.isBlank()) {
                _home.value = _home.value.copy(loading = false)
                return@launch
            }
            val d = runCatching { VodApiSource.fetchHome(base, okHttp, "movie", refresh = force) }
                .onFailure { android.util.Log.w("OTV", "home 拉取失败: ${it.javaClass.simpleName} ${it.message}") }
                .getOrNull()
            if (d == null || (d.sections.isEmpty() && d.carousel.isEmpty())) {
                android.util.Log.w("OTV", "home 空结果（源站不可达或解析为空），保留空态等待重试")
            }
            _home.value = if (d != null && (d.sections.isNotEmpty() || d.carousel.isNotEmpty())) {
                // 本地目录先上屏不能被上游整段覆盖：已装库时上游只补内容，不清掉本地板块。
                HomeState(
                    loading = false,
                    sections = mergeHomeSections(_home.value.sections, d.sections),
                    carousel = d.carousel,
                )
            } else {
                _home.value.copy(loading = false)
            }
        }
    }

    /**
     * 本地板块优先保留，板块内按 item id 去重合并上游条目。
     * 保持 fire-and-forget：上游结果异步追加，本地渲染路径不等待网络。
     */
    private fun mergeHomeSections(local: List<HSec>, upstream: List<HSec>): List<HSec> {
        if (local.isEmpty()) return upstream
        if (upstream.isEmpty()) return local
        val merged = local.toMutableList()
        upstream.forEach { section ->
            val index = merged.indexOfFirst { it.name == section.name }
            if (index < 0) {
                merged += section
            } else {
                val existing = merged[index]
                val seen = existing.items.mapTo(HashSet()) { it.id }
                merged[index] = existing.copy(
                    items = existing.items + section.items.filter { seen.add(it.id) },
                )
            }
        }
        return merged
    }

    /** 等待进行中的首页拉取结束（libBlocks「最近热门」块依赖 home sections） */
    suspend fun awaitHome() {
        refreshHome()
        homeJob?.join()
    }

    private fun localHomeSections(items: List<VodItem>): List<HSec> {
        val movies = items.filter { it.categoryId == "douban:movie" }
        val pool = movies.ifEmpty { items }
        return listOf(
            HSec("最近热门", pool.take(24)),
            HSec("最新上映", pool.sortedByDescending { it.year.toIntOrNull() ?: 0 }.take(24)),
            HSec("豆瓣高分", pool.sortedByDescending { it.rating }.take(24)),
        ).filter { it.items.isNotEmpty() }
    }

    /** Local metadata source for the fixed library sections; metadata rows are read popularity-first. */
    private fun localMetadataBlocks(category: String, rows: List<CatalogMedia>, imageProxyBase: String): List<HSec> {
        val categoryRows = rows.filter { media -> metadataCategoryKey(media.mediaType) == category }
        if (categoryRows.isEmpty()) return emptyList()

        fun section(name: String, selected: List<CatalogMedia>) = HSec(
            name,
            selected.take(24).map { VodApiSource.fromCatalogMedia(it, imageProxyBase) },
        )

        val byRelease = categoryRows.sortedWith(
            compareByDescending<CatalogMedia> { it.year.toIntOrNull() ?: 0 }
                .thenByDescending { it.updatedAt },
        )
        val byUpdate = categoryRows.asSequence()
            .filter { it.updatedAt > 0L }
            .sortedByDescending(CatalogMedia::updatedAt)
            .toList()
        return listOf(
            section("最近热门", categoryRows),
            section("最新上线", byRelease),
            section("最近更新", byUpdate),
        )
    }

    private fun metadataCategoryKey(mediaType: String): String = when (mediaType.trim().lowercase()) {
        "tv", "series", "tv_series", "show" -> "tv"
        "anime", "animation" -> "anime"
        "variety" -> "variety"
        "short" -> "short"
        else -> "movie"
    }

    /** 片库分类三块：固定「最近热门 / 最新上线 / 最近更新」，当前分类数据优先。 */
    private val _libBlocks = MutableStateFlow<Map<Int, List<HSec>>>(emptyMap())
    val libBlocksFlow: StateFlow<Map<Int, List<HSec>>> = _libBlocks.asStateFlow()

    /** 已完整尝试过三块拉取且【有产出】的分类（避免每次进页重复冷抓）。
     *  P2 修复（2026-09-04）：失败不再永久缓存——旧版无论成败都记 done，启动期瞬断/
     *  后端冷启动慢时三块内容整个会话为空且网络恢复也不补抓；现失败记时间戳做
     *  60s 退避重试（既自愈，又防源真挂时每次进页都冷抓三路） */
    private val libDone = java.util.concurrent.ConcurrentHashMap.newKeySet<Int>()
    private val libFailAt = java.util.concurrent.ConcurrentHashMap<Int, Long>()
    private val libJobs = java.util.concurrent.ConcurrentHashMap<Int, kotlinx.coroutines.Job>()

    /** 三栏空结果的自动补抓阶梯（毫秒）。
     *  2026-09-18（用户报障「要切一下页签才会有数据」）：热更重启内嵌后端的那次启动里，
     *  首轮 5 路三栏抓取整批连接拒绝；旧实现只记 libFailAt 就收工——页面上屏
     *  「暂无最近热门内容」，且 60s 内再进页也不重试，用户只能切页签/等一分钟才看到数据。
     *  现在失败即按阶梯后台补抓，成功立刻覆盖上屏，不需要用户任何操作。 */
    private val LIB_RETRY_DELAYS = longArrayOf(2_000L, 4_000L, 8_000L, 15_000L, 25_000L, 40_000L)
    private val libRetries = java.util.concurrent.ConcurrentHashMap<Int, Int>()
    private val libRetryJobs = java.util.concurrent.ConcurrentHashMap<Int, kotlinx.coroutines.Job>()

    /** 门闸等「三栏真的有内容」的轮询步长：判据读内存，2s 足够且不空转 CPU */
    private val LIB_CONTENT_POLL_MS = 2_000L

    /**
     * 等待指定分类的三块拉取 Job 完成（启动门闸深预载用）：
     * 幂等触发（已完成/进行中不重复拉），非 hhkan 分类（演示兜底/全部）直接返回。
     */
    suspend fun awaitLibBlocks(categoryId: String) {
        val cid = categoryNumber(categoryId.removePrefix("douban:")) ?: return
        libBlocks(categoryId)
        libJobs[cid]?.join()
    }

    /** 该分类三栏是否已有内容（启动门闸放行判据） */
    fun libBlocksHaveContent(cid: Int): Boolean =
        _libBlocks.value[cid].orEmpty().any { it.items.isNotEmpty() }

    /**
     * 启动门闸用：等该分类三栏【真的有内容】才返回；没有内容就绕过失败退避重抓，
     * 直到 [budgetMs] 预算用尽。
     *
     * 2026-09-18 用户报障「为什么不等界面有数据再进 APP，每次进来啥也没有」：
     * 旧实现门闸只等 Job 结束——首轮整批连接拒绝时 Job 同样算「结束」，门闸于是在空数据上
     * 放行，用户进页看到空三栏。返回 false 表示预算内仍没拿到内容（门闸按预算放行，
     * 剩余预载转后台，由 [LIB_RETRY_DELAYS] 阶梯继续补抓）。
     */
    suspend fun awaitLibBlocksContent(categoryId: String, budgetMs: Long = 45_000L): Boolean {
        val cid = categoryNumber(categoryId.removePrefix("douban:")) ?: return false
        val deadline = System.currentTimeMillis() + budgetMs
        while (true) {
            libBlocks(categoryId, force = true)
            libJobs[cid]?.join()
            if (libBlocksHaveContent(cid)) return true
            if (System.currentTimeMillis() + LIB_CONTENT_POLL_MS >= deadline) return false
            kotlinx.coroutines.delay(LIB_CONTENT_POLL_MS)
        }
    }

    /** 片库固定三栏；首页的 Douban 精选栏目由 homeFlow 独立提供。 */
    private val blockOrder = listOf("最近热门", "最新上线", "最近更新")
    private fun completeBlocks(sections: List<HSec>): List<HSec> =
        blockOrder.map { name -> sections.firstOrNull { it.name == name } ?: HSec(name, emptyList()) }

    /**
     * 需求④（2026-09-07 光标跳行修复）：分类板块「热门块就绪」标记。
     * hot 依赖 home 抓取最慢；旧版逐块到货即上屏 + 等待期兜底网格顶替，
     * LazyColumn 结构两次大挪移 = 焦点落「最新上线」后「最近热门」再插到上方，
     * 用户看到「按下键直接滚到最新上线跳过了最近热门」。UI 在本标记置位前
     * 只渲染占位（不渲染部分板块/兜底网格），三块一次性完整出现。
     */
    private val _hotSettled = MutableStateFlow<Set<Int>>(emptySet())
    val hotSettledFlow: StateFlow<Set<Int>> = _hotSettled.asStateFlow()
    private fun markHotSettled(cid: Int) {
        _hotSettled.value = _hotSettled.value + cid
    }

    /** Read the selected category from the local metadata catalogue; network refresh is handled separately. */
    fun libBlocks(categoryId: String) {
        libBlocks(categoryId, force = false)
    }

    /** [force]=true 绕过 60s 失败退避（门闸等待 / 自动补抓专用）。 */
    fun libBlocks(categoryId: String, force: Boolean) {
        val category = categoryId.removePrefix("douban:")
        val cid = categoryNumber(category) ?: return
        // 进行中的 Job 一律复用（门闸重进/自动补抓都会再调一次并 join 它）；
        // force 只绕过「本类已完成」与失败退避。
        if (libJobs[cid]?.isActive == true) return
        if (!force && cid in libDone) return
        // 失败退避：距上次全挂不足 60s 不重试（防源真挂时每次进页都冷抓）
        val failAt = libFailAt[cid]
        if (!force && failAt != null && System.currentTimeMillis() - failAt < 60_000L) return
        libJobs[cid] = repoScope.launch {
            val base = settings.current().hhkanBaseUrl
            val localRows = runCatching {
                (metadataCatalog.topRows(category, "hot") + metadataCatalog.topRows(category, "new") + metadataCatalog.topRows(category, "updated"))
                    .distinctBy(CatalogMedia::mytvId)
            }.getOrDefault(emptyList())
            val localSections = localMetadataBlocks(category, localRows, base)
            val hasLocalSections = localSections.any { it.items.isNotEmpty() }
            val installedMetadata = runCatching { metadataCatalog.hasInstalledDatabase() }.getOrDefault(false)
            _libBlocks.value = _libBlocks.value + (cid to completeBlocks(localSections))
            if (hasLocalSections && installedMetadata) {
                libDone += cid
                libFailAt.remove(cid)
                libRetries.remove(cid)
            } else if (hasLocalSections) {
                // The packaged seed is an immediate offline fallback. Re-read after catalogue refresh
                // so an installed SQLite package replaces those seed rows without delaying first paint.
                scheduleLibRetry(cid)
            } else {
                libFailAt[cid] = System.currentTimeMillis()
                scheduleLibRetry(cid)
            }
            markHotSettled(cid)
        }
        // Timeout marker remains as a guard if catalogue access stalls during storage recovery.
        repoScope.launch {
            kotlinx.coroutines.delay(8_000L)
            markHotSettled(cid)
        }
    }

    /** 三栏空结果的后台自动补抓：按 [LIB_RETRY_DELAYS] 阶梯重试，成功即停（libDone 收敛）。 */
    private fun scheduleLibRetry(cid: Int) {
        val attempt = libRetries.getOrDefault(cid, 0)
        if (attempt >= LIB_RETRY_DELAYS.size) return
        if (libRetryJobs[cid]?.isActive == true) return
        val wait = LIB_RETRY_DELAYS[attempt]
        libRetries[cid] = attempt + 1
        android.util.Log.w("OTV", "分类 $cid 三栏为空，${wait}ms 后自动补抓（第 ${attempt + 1} 次）")
        libRetryJobs[cid] = repoScope.launch {
            kotlinx.coroutines.delay(wait)
            libBlocks(categoryKey(cid), force = true)
            libJobs[cid]?.join()
        }
    }

    /** 首页只作热门栏的同分类补位，短剧不再误拿全站“最近更新”。 */
    private fun hotFromHome(cid: Int): List<VodItem> {
        val hints = LIB_HOT_SECS[cid] ?: return emptyList()
        val secs = _home.value.sections
        val hit = secs.firstOrNull { sec -> hints.any { sec.name == it || sec.name.contains(it) } }
        return hit?.items.orEmpty()
    }

    /** hero 元信息（原版 vod-cat「2026 / 日本 / 冒险…」）：懒解析详情 meta，非懒源用字段拼串 */
    suspend fun vodMeta(vodId: String): String = withContext(Dispatchers.IO) {
        val item = itemById(vodId) ?: return@withContext ""
        if (item.detailRef.isNotBlank()) {
            fetchDetailCached(item.detailRef)?.meta?.takeIf { it.isNotBlank() } ?: item.meta.ifBlank { item.year }
        } else {
            item.meta.ifBlank { (listOf(item.year, item.area).filter { it.isNotBlank() } + item.tags).joinToString(" / ") }
        }
    }

    /** hero 简介补全（原版 showVod：异步补 meta/desc/评分；目录外条目如 carousel 直接按 ref 解析） */
    private val briefCache = java.util.concurrent.ConcurrentHashMap<String, Triple<String, String, Double>>()

    /**
     * 简介专用客户端（读超时 20s）。
     *
     * 2026-09-18（用户报障「简介加载不出来」）：旧实现与 [fetchDetailCached] 共用全量
     * `/vod/detail`——后端得为它把 22 个站点的搜索+探测全跑完（本机实测 7.5s，电视弱网
     * 远超客户端 45s 读超时），hero 于是长时间停在「加载简介…」，超时后又退化成
     * 「XXX · My TV 精选内容，一键播放。」这种通用占位文案，真正的豆瓣简介永远上不了屏。
     */
    private val briefClient by lazy {
        okHttp.newBuilder().readTimeout(20, java.util.concurrent.TimeUnit.SECONDS).build()
    }

    /**
     * 简介专用详情缓存：**不可复用 [detailInfoCache]**——`defer_sources=1` 的响应
     * `sources` 恒为空，写进共用缓存会让详情页/播放器以为该片没有线路（「暂无片源」）。
     */
    private val briefDetailCache = java.util.concurrent.ConcurrentHashMap<String, DetailInfo>()

    private suspend fun fetchBriefDetail(ref: String): DetailInfo? {
        briefDetailCache[ref]?.let { return it }
        val d = runCatching {
            VodApiSource.fetchDetail(settings.current().hhkanBaseUrl, briefClient, ref, deferSources = true)
        }.onFailure { android.util.Log.w("OTV", "hero 简介拉取失败 ref=$ref : ${it.message}") }
            .getOrNull() ?: return null
        briefDetailCache[ref] = d
        return d
    }

    suspend fun vodBrief(vodId: String): Triple<String, String, Double>? = withContext(Dispatchers.IO) {
        briefCache[vodId]?.let { return@withContext it }
        val item = itemById(vodId)
        val ref = item?.detailRef?.takeIf { it.isNotBlank() }
            ?: vodId.removePrefix("douban:").takeIf { vodId.startsWith("douban:") }
            ?: return@withContext null
        val itemMeta = item
            ?.let { (listOf(it.year, it.area).filter { b -> b.isNotBlank() } + it.tags).joinToString(" / ") }
            .orEmpty()
        val d = fetchBriefDetail(ref)
        if (d == null) {
            // 失败不写 briefCache（翻回这个 hero 还能重试），但条目自带的元数据先顶上
            return@withContext item?.let { Triple(itemMeta, it.desc, it.rating) }
        }
        val meta = d.meta.takeIf { it.isNotBlank() } ?: itemMeta
        Triple(meta, d.desc, if (item != null && item.rating > 0) item.rating else d.rating)
            .also { briefCache[vodId] = it }
    }

    companion object {
        /**
         * 分类 id → 片库三栏 cid（1 电影 / 2 电视剧 / 3 动漫 / 4 综艺 / 6 短剧）。
         *
         * 2026-09-15 修复：目录切豆瓣后分类 id 是 `douban:movie` 形态，而首页按旧口径
         * `removePrefix("hhkan:").toIntOrNull()` 解析——结果恒为 null，三栏恒空，影视 tab
         * 一直显示「暂无内容」（数据其实已由 libBlocks 拉到 libBlocksFlow 里）。这里与
         * [libBlocks] 用同一套解析，旧 `hhkan:<数字>` 形态继续兼容。
         */
        fun categoryCid(categoryId: String): Int? {
            val raw = categoryId.removePrefix("douban:").removePrefix("hhkan:")
            return categoryNumber(raw) ?: raw.toIntOrNull()
        }

        private fun categoryNumber(key: String): Int? = when (key) {
            "movie" -> 1; "tv" -> 2; "anime" -> 3; "variety" -> 4; "short" -> 6; else -> null
        }
        private fun categoryKey(cid: Int): String = when (cid) {
            2 -> "tv"; 3 -> "anime"; 4 -> "variety"; 6 -> "short"; else -> "movie"
        }
        /** 与网页一致：分类 → 首页同分类热门板块标题候选。 */
        val LIB_HOT_SECS = mapOf(
            1 to listOf("近期热门电影", "热门电影"),
            2 to listOf("近期热门剧集", "热门剧集"),
            3 to listOf("热播动漫", "热门动漫"),
            4 to listOf("热播综艺纪录", "热门综艺"),
            6 to listOf("近期热门短剧", "热门短剧"),
        )

        /** 直链解析缓存 TTL：超过即重解析（签名类直链 ~20 分钟档过期；解析本身 <1s 不贵） */
        const val PLAY_URL_TTL_MS = 15 * 60_000L
    }

    // ---------- 全部影视页（原版 view-all：筛选 + 分页海报墙） ----------

    /** 筛选项会话缓存（需求⑯ 2026-09-11）：筛选项内容基本静态，重复进页/切类不再等网络。
     *  旧版每次进「全部」页/切类别都全量重拉（冷启动后端未就绪时还白吃 4×2.5s 重试）。 */
    private val filtersCache = java.util.concurrent.ConcurrentHashMap<Int, HhkanSource.FilterOptions>()

    suspend fun allFilters(cid: Int = 1): HhkanSource.FilterOptions = withContext(Dispatchers.IO) {
        val local = metadataCatalog.filterOptions(categoryKey(cid))
        if (local.genres.isNotEmpty() || local.regions.isNotEmpty() || local.years.isNotEmpty()) {
            val options = HhkanSource.FilterOptions(
                types = local.genres,
                areas = local.regions,
                years = local.years,
                ratings = listOf("9", "8", "7", "6"),
            )
            if (options.types.isNotEmpty() || options.areas.isNotEmpty() || options.years.isNotEmpty()) {
                filtersCache[cid] = options
                return@withContext options
            }
        }
        filtersCache[cid]?.let { return@withContext it }
        // 内置后端冷启动期连接拒绝会被 runCatching 吞成空筛选（UI 再白吃 2.5s×4 重试）——
        // 先等 /api/health 就绪（已就绪时毫秒级通过）
        runCatching { waitBackendReady(settings.current().hhkanBaseUrl) }
        // 需求 我的#8：筛选选项必须按所选类别拉取（源站各频道筛选项不同），
        // 旧版固定 /hhkan/filters/1 导致选「短剧」等类别后筛选结果与源站不一致
        val f = runCatching { VodApiSource.fetchFilters(settings.current().hhkanBaseUrl, okHttp, categoryKey(cid)) }
            .getOrDefault(HhkanSource.FilterOptions())
        if (f.types.isNotEmpty()) filtersCache[cid] = f
        f
    }

    suspend fun showPage(
        cid: Int,
        type: String = "",
        area: String = "",
        lang: String = "",
        year: String = "",
        rating: String = "",
        by: String = "3",
        page: Int = 1,
    ): HhkanSource.ShowPage? = withContext(Dispatchers.IO) {
        val installedMetadata = metadataCatalog.hasInstalledDatabase()
        val localPage = metadataCatalog.browsePage(
            mediaType = categoryKey(cid), genre = type, region = area, year = year,
            minimumRating = Regex("\\d+(?:\\.\\d+)?").find(rating)?.value?.toDoubleOrNull(),
            sort = when (by.lowercase()) { "2", "new" -> "new"; "1", "rating" -> "rating"; else -> "hot" },
            page = page,
            pageSize = 48,
        )
        if (installedMetadata || localPage.items.isNotEmpty()) {
            return@withContext HhkanSource.ShowPage(
                items = localPage.items.map { VodApiSource.fromCatalogMedia(it, settings.current().hhkanBaseUrl) },
                hasMore = page.coerceAtLeast(1) * 48 < localPage.total,
                upstreamAvailable = true,
            )
        }
        runCatching { waitBackendReady(settings.current().hhkanBaseUrl) }
        val base = settings.current().hhkanBaseUrl
        if (base.isBlank()) return@withContext null
        val sort = when (by) { "2", "new" -> "new"; "1", "rating" -> "rating"; else -> "hot" }
        runCatching { VodApiSource.fetchShow(base, okHttp, categoryKey(cid), type, area, year, rating, sort, page) }.getOrNull()
    }

    suspend fun douban(query: String): DoubanInfo? = withContext(Dispatchers.IO) {
        runCatching {
            val q = java.net.URLEncoder.encode(query.trim(), "UTF-8")
            val req = okhttp3.Request.Builder()
                .url(settings.current().hhkanBaseUrl.trimEnd('/') + "/api/douban?q=$q")
                .header("User-Agent", "Mozilla/5.0 (Linux; Android 11) OptimalTV/1.0")
                .build()
            okHttp.newCall(req).execute().use { resp ->
                require(resp.isSuccessful) { "HTTP ${resp.code}" }
                val obj = org.json.JSONObject(resp.body?.string().orEmpty())
                DoubanInfo(
                    found = obj.optBoolean("found", false),
                    rating = if (obj.isNull("rating")) "" else obj.optString("rating"),
                    desc = if (obj.isNull("desc")) "" else obj.optString("desc"),
                    poster = if (obj.isNull("poster")) "" else obj.optString("poster"),
                )
            }
        }.getOrNull()
    }

    fun byCategory(catId: String): List<VodItem> =
        _state.value.catalog?.dedupedItems?.filter { catId.isBlank() || it.categoryId == catId } ?: emptyList()

    // ---------- 懒解析（hhkan 源：目录无选集，详情/起播时经原版后端解析） ----------

    private val detailCache = java.util.concurrent.ConcurrentHashMap<String, VodItem>()
    /** 原始详情（含全部线路选集）缓存：resolveDetail 与 resolveLines 共用同一次
     *  /hhkan/detail 抓取——旧版 resolveLines 再抓一次详情页，起播平白多一整轮
     *  网络往返（低端机+冷抓 20s+ 时翻倍），v1.10 去重 */
    private val detailInfoCache = java.util.concurrent.ConcurrentHashMap<String, DetailInfo>()
    /** 已为播放会话拉取过完整补充源的好好看 vid。 */
    private val playbackDetailReady = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()

    private suspend fun fetchDetailCached(ref: String, forceRefresh: Boolean = false): DetailInfo? {
        val cached = detailInfoCache[ref]
        if (!forceRefresh && cached != null) return cached
        val d = runCatching {
            VodApiSource.fetchDetail(
                settings.current().hhkanBaseUrl, detailClient, ref, refresh = forceRefresh)
        }
            .onFailure { android.util.Log.w("OTV", "fetchDetail 失败 ref=$ref : ${it.message}") }
            .getOrNull()
            ?: return cached
        if (d.lines.isNotEmpty()) detailInfoCache[ref] = d
        return if (d.lines.isNotEmpty() || cached == null) d else cached
    }

    /** 解析详情：拉选集（默认线路）+ 元数据补全；非懒源原样返回 */
    suspend fun resolveDetail(vodId: String, forceRefresh: Boolean = false): VodItem? = withContext(Dispatchers.IO) {
        val cached = detailCache[vodId]
        if (!forceRefresh && cached != null) return@withContext cached
        // 目录外条目兜底（实时搜索结果等）：hhkan: 前缀直接构造裸 item 走懒解析
        val localMedia = when {
            vodId.startsWith("mytv:") -> metadataCatalog.findByMyTvId(vodId.removePrefix("mytv:"))
            vodId.startsWith("douban:") -> metadataCatalog.findByLegacyDoubanId(vodId)
            else -> null
        }
        val item = itemById(vodId) ?: localMedia?.let { VodApiSource.fromCatalogMedia(it, settings.current().hhkanBaseUrl) }
            ?: if (vodId.startsWith("douban:")) {
            com.qiubo.optimaltv.data.model.VodItem(
                id = vodId, sourceId = "douban", title = "", categoryId = "",
                detailRef = vodId.removePrefix("douban:"),
            )
        } else return@withContext null
        if (item.detailRef.isBlank()) return@withContext item
        val d = fetchDetailCached(item.detailRef, forceRefresh) ?: return@withContext (cached ?: item)
        // 集数网格展示默认线路（第一条）的选集；全部线路在播放器内换线
        val eps = d.lines.firstOrNull()?.episodes?.mapIndexed { i, (name, _, _) ->
            Episode(i, name.ifBlank { "第${i + 1}集" }, "")
        }.orEmpty()
        item.copy(
            // 目录外条目（轮播/搜索结果/最新上线等）裸 item 无标题：从详情回填，
            // 否则详情页与「最近观看」卡片全部缺片名
            title = item.title.ifBlank { d.title },
            episodes = eps,
            year = item.year.ifBlank { d.year },
            desc = item.desc.ifBlank { d.desc },
            posterUrl = item.posterUrl.ifBlank { d.poster },
            rating = if (item.rating > 0) item.rating else d.rating,
            meta = item.meta.ifBlank { d.meta },
            actors = item.actors.ifBlank { d.actors },
        ).also { if (eps.isNotEmpty()) detailCache[vodId] = it }
    }

    /**
     * 首屏片源预热（2026-09-18 用户需求「片源加载好后才能进入 app」）：
     * 启动门闸里先把首屏最可能点到的影片线路匹配跑完——[resolveDetail] 的结果落
     * detailCache/detailInfoCache，详情页进页即命中，主按钮直接是「立即播放」，
     * 不再出现「正在查找片源」的等待态。返回「线路就绪（有选集）」的条数。
     *
     * [batch] 必须限并发：一次详情匹配在服务端要同时打多个影视源并探测线路（实测单条冷
     * 路径 12~30s），一次全放出去会被上游限流、整体反而更慢，也会和门闸里其它预载抢出口。
     * 按 [ids] 顺序分批——排在前面的（hero 第一条）先跑，用户最可能先点的那条最早就绪。
     */
    suspend fun preloadDetailSources(ids: List<String>, batch: Int = 2): Int {
        val targets = ids.filter { it.isNotBlank() }.distinct()
        if (targets.isEmpty()) return 0
        val size = batch.coerceAtLeast(1)
        var ready = 0
        for (chunk in targets.chunked(size)) {
            val results = coroutineScope {
                chunk.map { id ->
                    async {
                        runCatching { resolveDetail(id) }
                            .onFailure { android.util.Log.w("OTV", "首屏片源预热失败 id=$id : ${it.message}") }
                            .getOrNull()
                    }
                }.awaitAll()
            }
            ready += results.count { !it?.episodes.isNullOrEmpty() }
        }
        return ready
    }

    /** 构建当前集的可换线路列表：普通源取 episodes 直链；hhkan 源解析多线路（url 懒填） */
    suspend fun resolveLines(item: VodItem, epIndex: Int): List<LineInfo> = withContext(Dispatchers.IO) {
        if (item.detailRef.isBlank()) {
            return@withContext siblings(item.normTitle)
                .filter { epIndex < it.episodes.size }
                .map { s ->
                    val ep = s.episodes[epIndex]
                    LineInfo(s.id, "${s.sourceId} · ${ep.name}", ep.url)
                }
        }
        val full = resolveDetail(item.id) ?: return@withContext emptyList()
        if (epIndex >= full.episodes.size) return@withContext emptyList()
        // 详情页用快速主数据，播放器起播前补一次 enrich=1：保证首次打开时
        // 新增站点也已进入自动选源池，不被详情页的基础响应永久缓存。
        var d = detailInfoCache[item.detailRef] ?: fetchDetailCached(item.detailRef)
            ?: return@withContext emptyList()
        d.lines.mapNotNull { line ->
            val ep = line.episodes.getOrNull(epIndex) ?: return@mapNotNull null
            LineInfo(
                vodId = item.id,
                label = line.name,
                url = "",
                playRef = "${item.detailRef}|${ep.second}|${ep.third}",
            )
        }
    }

    // ---------- 云端 Resolver（L0~L5 取源阶梯） ----------

    /**
     * 一次取源的结果 + 走了哪一级（用于埋点与排障）。
     *
     * [lines] 为空表示这一层没拿到任何可播线路，调用方必须继续往下走，
     * **绝不能让用户看到空片源**。
     */
    data class ResolvedLines(
        val lines: List<LineInfo> = emptyList(),
        val tier: String = "none",
        val resolve: SourceResolveResult? = null,
    )

    /**
     * 取源阶梯（**执行顺序**见 [VodRepositoryConfig.TIER_ORDER]）：
     * - 端侧 `/vod/detail` 多源匹配（实测 ~0.9s 出 12 条线）——**最先跑**，唯一能造出可播线路的一层；
     * - L0 本地 last_good（同影片同集上次成功线路）——在已取到的线路里零网络命中；
     * - L1 云端 Resolver（按元数据中心 ID 的线路排序）——**最后跑，且有 1.5s 硬上限**。
     *
     * 顺序是刻意的：云端 Resolver 排在端侧之后，是因为它只是**排序层**——
     * 候选 (source_id:content_id) 与 detail 线路 id 前两段同构，只能重排既有线路，
     * 造不出可播线路。把它排在前面串行等，机房出口不通时（磐石云无境外出口）
     * 每次点播都要先白等一个超时，方案「客户端优先直连、云端失败不拖起播」就废了。
     *
     * [ResolvedLines.tier] 记录实际生效层；任一层拿不到可用结果都回落到端侧原序，
     * **绝不让用户看到空片源**。
     */
    suspend fun resolveLinesWithResolver(
        item: VodItem,
        epIndex: Int,
        mytvId: String = item.mytvId,
        episode: Int = epIndex + 1,
    ): ResolvedLines = withContext(Dispatchers.IO) {
        val mytvIdLong = mytvId.trim().toLongOrNull()

        // L2 先跑（端侧既有 detail 路径）——它是**唯一能凭空造出可播线路**的一层，
        // 也是本机可达、不依赖机房出口的一层。云端 Resolver 只是它之上的排序层。
        // 旧序把云端排在本地前面、串行等它：机房到片源出口不通时（磐石云无境外出口），
        // 每次点播都要先白等一个 /vod/resolve 超时才回落，方案「客户端优先直连、
        // 云端失败不拖起播」被完全架空。改为：**本地先拿到线，再并行探云端排序**。
        val local = resolveLines(item, epIndex)
        if (local.isEmpty()) return@withContext ResolvedLines(emptyList(), "none", null)

        // L0：本地 last_good（同影片同集上次成功线路，零网络）
        if (mytvIdLong != null) {
            val remembered = runCatching {
                Graph.db.lastGoodSourceDao().get(mytvIdLong, epIndex)
            }.getOrNull()
            if (remembered != null) {
                // 在已取到的 local 里找，不再为 L0 重新触发一整轮 /vod/detail。
                // 逐段比对而非 startsWith：`content-1` 会前缀命中 `content-10`，
                // 那样 L0 会把「上次成功的第 10 集线路」当成这集的线路送进起播——错集。
                val hit = local.firstOrNull { line ->
                    ResolverRanking.matchesCandidate(
                        lineLinePrefix(line.playRef), remembered.sourceId, remembered.contentId,
                    )
                }
                if (hit != null) {
                    PlaybackTelemetry.counters.inc("resolve.tier.l0")
                    return@withContext ResolvedLines(listOf(hit), "L0_last_good")
                }
            }
        }

        // L1：云端 Resolver（只做排序，不新增可播线路）。**短超时**：它是纯锦上添花的
        // 排序层，超时不得影响起播——超了就用 local 原序（L2），与旧版功能等价。
        if (mytvIdLong != null) {
            val result = runCatching {
                withTimeout(VodRepositoryConfig.RESOLVER_TIMEOUT_MS) {
                    VodApiSource.fetchResolve(
                        settings.current().hhkanBaseUrl, resolveClient, mytvIdLong, episode,
                    )
                }
            }.getOrNull()
            if (result != null && result.hasCandidates) {
                val ranked = applyResolverRanking(local, result)
                if (ranked.isNotEmpty()) {
                    PlaybackTelemetry.counters.inc("resolve.tier.l1")
                    return@withContext ResolvedLines(ranked, "L1_cloud_resolver", result)
                }
            }
        }

        // 云端没给出可用排序（或超时/不可达）→ 端侧原有顺序，行为与旧版一致。
        PlaybackTelemetry.counters.inc("resolve.tier.l2")
        return@withContext ResolvedLines(VodRepositoryConfig.onCloudUnavailable(local), "L2_local_detail")
    }

    /**
     * 把 Resolver 的候选排序套到 detail 已解析出的线路上。
     *
     * 匹配不上的候选直接忽略（detail 才是可播线路的真相来源），
     * 没被 resolver 点名的线路保持在原顺序排后面，保证不丢线路。
     *
     * 顺序与匹配规则由 [ResolverRanking] 承担（纯函数、可单测）：preferred 必须在首位，
     * backups 保持后端次序，且按 `{sourceId}:{contentId}` 逐段比对。
     */
    private fun applyResolverRanking(
        all: List<LineInfo>,
        result: SourceResolveResult,
    ): List<LineInfo> {
        if (!result.usable) return emptyList()
        if (all.isEmpty()) return emptyList()
        val ranked = ResolverRanking.order(
            linePrefixes = all.map { lineLinePrefix(it.playRef) },
            preferred = result.preferred,
            backups = result.backups,
        ).mapNotNull { all.getOrNull(it) }
        return ranked
    }

    /**
     * playRef = `"{detailRef}|{lineId}|{episode}"`，取 [LineInfo.id] 段的前两段
     * （`{sourceId}:{contentId}`）作为候选匹配前缀。直链线路没有 `{a}:{b}` 形态时返回空串，
     * [ResolverRanking] 会跳过它——直链线路不该被云端候选改名换掉。
     */
    private fun lineLinePrefix(playRef: String): String {
        if (playRef.isBlank()) return ""
        val lineId = playRef.split("|").getOrNull(1) ?: return ""
        val parts = lineId.split(':')
        if (parts.size < 2) return ""
        return "${parts[0]}:${parts[1]}"
    }

    /** 记录某集最近成功的线路（L0 的写侧）。只存稳定引用，不存播放地址/凭证。 */
    suspend fun rememberLastGoodSource(
        mytvId: String,
        epIndex: Int,
        sourceId: String,
        contentId: String,
    ) {
        val id = mytvId.trim().toLongOrNull() ?: return
        if (sourceId.isBlank() || contentId.isBlank()) return
        runCatching {
            Graph.db.lastGoodSourceDao().upsert(
                LastGoodSourceEntity(
                    mytvId = id,
                    episodeIndex = epIndex,
                    sourceId = sourceId,
                    contentId = contentId,
                    successfulAt = System.currentTimeMillis(),
                ),
            )
        }
    }

    /** 直链缓存条目：raw=上游直链 + 解析时刻（点播部分 CDN 直链带分钟级签名，须按 TTL 失效） */
    private class PlayEntry(val raw: String, val ts: Long)
    private val playCache = java.util.concurrent.ConcurrentHashMap<String, PlayEntry>()

    /** 直链缓存失效（失败/卡顿重解析前调用：确保拿到带新签名的地址） */
    fun bustPlayCache(playRef: String) {
        if (playRef.isNotBlank()) playCache.remove(playRef)
    }

    /** 点播中继包裹：ref=none——App 直连历来无 Referer，异站 Referer 可能被 CDN 拒 */
    private fun relayWrap(base: String, raw: String): String =
        "$base/api/relay?u=" + java.net.URLEncoder.encode(raw, "UTF-8") + "&ref=none"

    /**
     * 解析线路直链（v1.14 起默认经本地中继多连接并行拉片）：
     * ExoPlayer 的 HLS 是单加载线程顺序拉分片，免费 CDN 单连接供需比实测仅 0.86
     * （155KB/s 供给 vs ~180KB/s 码率）——90s 缓冲以 ~14%/实时 速率放空，
     * 11~20 分钟后进入周期性 rebuffer，即「点播 20 分钟后频繁卡顿」的根因。
     * 中继清单感知预取等效 4~6 连接并行（实测 571KB/s，3.7×）。
     * preferRelay=false：该线路中继路径已被判不可用（PlayerViewModel 阶梯），退回直连。
     */
    suspend fun resolvePlayUrl(line: LineInfo, preferRelay: Boolean = true): String = withContext(Dispatchers.IO) {
        if (line.playRef.isBlank()) return@withContext line.url
        val now = System.currentTimeMillis()
        val hit = playCache[line.playRef]
        val base = settings.current().hhkanBaseUrl
        val raw: String
        if (hit != null && now - hit.ts < PLAY_URL_TTL_MS) {
            raw = hit.raw
        } else {
            val parts = line.playRef.split("|")
            if (parts.size != 3) return@withContext ""
            val fetched = runCatching {
                VodApiSource.fetchPlay(base, okHttp, parts[0], parts[1], parts[2]).firstOrNull().orEmpty()
            }.getOrNull()?.takeIf { it.isNotBlank() }
            if (fetched != null) {
                playCache[line.playRef] = PlayEntry(fetched, now)
                raw = fetched
            } else {
                // 解析失败降级旧缓存（好过空串直接判线失败）
                raw = hit?.raw.orEmpty()
            }
        }
        when {
            raw.isBlank() -> ""
            preferRelay -> relayWrap(base, raw)
            else -> raw
        }
    }

    // ---------- 目录磁盘快照（v1.10 低端机秒开，2026-08-31） ----------
    // 冷启动链路：内置 python 后端起 2~5s + 目录冷抓 5~20s → 影视页转圈久。
    // 快照把上次成功目录立即上屏（loading=false），后台 refresh 拿到新数据自动替换。
    // v1.20（2026-09-03）：快照缺失（首装）时回退 assets/catalog_seed.json 种子——
    // 首次进入 app 5s 门闸内也有完整目录可上屏，海报走 Coil 网络流式加载。
    private val snapshotFile: java.io.File
        get() = java.io.File(appContext.filesDir, "catalog_snapshot.json")

    init {
        repoScope.launch {
            runCatching { restoreSnapshot() }
                .onFailure { android.util.Log.w("OTV", "目录快照恢复失败: ${it.message}") }
        }
    }

    /** 目录来源候选：标签 + JSON 文本 + 该来源允许的快照版本集合。 */
    private data class SnapshotCandidate(
        val label: String,
        val text: String,
        val acceptedVersions: Set<Int>,
    )

    private fun readSnapshotCandidates(metadataInstalled: Boolean): List<SnapshotCandidate> = buildList {
        val f = snapshotFile
        if (CatalogSnapshotPolicy.shouldReadLegacy(metadataInstalled, f.length())) {
            runCatching { f.readText() }.getOrNull()?.let {
                add(SnapshotCandidate("磁盘快照", it, CatalogOriginPolicy.PERSISTED_ACCEPTED_VERSIONS))
            }
        }
        // 磁盘快照过旧、损坏或混入补充源时也必须回退到可信种子，而不是空屏。
        // 种子用 SEED_ACCEPTED_VERSIONS：它是随包发布的静态资源，v3 仍可读（见 CatalogOriginPolicy 注释）。
        runCatching {
            appContext.assets.open(CatalogSeedAsset.NAME).use { it.readBytes().toString(Charsets.UTF_8) }
        }.getOrNull()?.let {
            add(SnapshotCandidate("内置种子", it, CatalogOriginPolicy.SEED_ACCEPTED_VERSIONS))
        }
    }

    private suspend fun restoreSnapshot() {
        if (metadataCatalog.hasInstalledDatabase()) return
        var restored: Pair<String, CatalogSnapshot>? = null
        for (candidate in readSnapshotCandidates(metadataInstalled = false)) {
            val decoded = runCatching {
                decodeSnapshot(candidate.text, candidate.acceptedVersions)
            }.getOrNull()
            if (decoded != null) {
                restored = candidate.label to decoded
                break
            }
            android.util.Log.w("OTV", "${candidate.label} 已损坏或含非好好看卡片，跳过")
        }
        val (label, snapshot) = restored ?: return
        val cats = snapshot.categories
        val items = snapshot.items
        val groups = items.groupBy { it.normTitle }
        // 仅当内存还没有目录时上屏（refresh 先完成则不打扰，避免旧数据覆盖新数据）
        if (_state.value.catalog == null) {
            _state.value = CatalogState(loading = false, catalog = Aggregated(cats, groups.map { it.value.first() }, groups))
            android.util.Log.i("OTV", "$label 恢复上屏：${items.size} 条 / ${cats.size} 分类（后台刷新中）")
        }
    }

    private fun writeSnapshot(cats: List<Category>, items: List<VodItem>) {
        require(items.all { CatalogOriginPolicy.allows(it.sourceId, it.id) }) {
            "目录快照只能包含好好看卡片"
        }
        val o = org.json.JSONObject()
        o.put("v", CatalogOriginPolicy.SNAPSHOT_VERSION)
        o.put("ts", System.currentTimeMillis())
        o.put("categories", org.json.JSONArray().apply { cats.forEach { put(org.json.JSONObject().put("id", it.id).put("name", it.name)) } })
        var persistedItems = CatalogSnapshotPolicy.boundedItems(items)
        var encoded: String
        do {
            o.put(
                "items",
                org.json.JSONArray().apply {
                    persistedItems.forEach {
                    put(
                        org.json.JSONObject()
                            .put("id", it.id).put("s", it.sourceId).put("t", it.title)
                            .put("c", it.categoryId).put("y", it.year).put("ar", it.area)
                            .put("r", it.rating).put("p", it.posterUrl).put("h", it.heroUrl)
                            .put("rm", it.remark).put("d", it.detailRef)
                            .put("mytv_id", it.mytvId).put("q", org.json.JSONArray(it.searchTerms)),
                    )
                }
                },
            )
            encoded = o.toString()
            if (encoded.toByteArray(Charsets.UTF_8).size <= CatalogSnapshotPolicy.MAX_BYTES || persistedItems.isEmpty()) break
            persistedItems = persistedItems.dropLast((persistedItems.size / 4).coerceAtLeast(1))
        } while (true)
        if (!CatalogSnapshotPolicy.isReadableSize(encoded.toByteArray(Charsets.UTF_8).size.toLong())) return
        // 临时文件 + 原子改名：写一半崩溃/断电不会留下半个快照
        val tmp = java.io.File(snapshotFile.parentFile, snapshotFile.name + ".tmp")
        tmp.writeText(encoded)
        if (!tmp.renameTo(snapshotFile)) {
            snapshotFile.delete()
            tmp.renameTo(snapshotFile)
        }
        android.util.Log.i("OTV", "目录快照落盘：${persistedItems.size} 条")
    }
}

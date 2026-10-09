package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.BuildConfig
import com.qiubo.optimaltv.data.catalog.MetadataCatalogStore
import com.qiubo.optimaltv.data.model.Category
import com.qiubo.optimaltv.data.model.CatalogMedia
import com.qiubo.optimaltv.data.model.Episode
import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.data.source.ContentSource
import com.qiubo.optimaltv.data.source.HhkanSource
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
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

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

/** Legacy JSON/API catalog search must use every distinct row, not the title-collapsed browse list. */
internal fun searchLegacyCatalog(catalog: Aggregated?, query: String): List<VodItem> {
    val q = query.trim()
    if (q.isEmpty()) return emptyList()
    return catalog?.groups?.values?.flatten()?.distinctBy(VodItem::id)?.filter { item ->
        item.title.contains(q, true) || item.tags.any { it.contains(q, true) }
                || item.year == q || item.area.contains(q, true)
                || item.searchTerms.any { it.contains(q, true) }
    } ?: emptyList()
}

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

private fun CatalogMedia.localIdentityKey(): String =
    mytvId.trim().takeIf(String::isNotEmpty) ?: legacyRef ?: title.trim()

/** A populated Douban section replaces the fallback completely, preserving its ranking. */
internal fun preferDoubanSections(local: List<HSec>, upstream: List<HSec>): List<HSec> {
    val result = local.toMutableList()
    upstream.filter { it.items.isNotEmpty() }.forEach { section ->
        val index = result.indexOfFirst { it.name == section.name }
        if (index < 0) result += section else result[index] = section
    }
    return result
}

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

/**
 * 起播线路的 L0→L5 取源阶梯（2026-09-28）。
 *
 * 目标只有一个：**尽快拿到一条能播的线路**。每一级都比下一级便宜，所以按「便宜且可靠」
 * 从上往下走；任一级拿到线路就停。
 *
 *  - L0 本地 last_good —— 纯 Room 读（微秒级）。上次这集成功播过，最可信；
 *  - L1 云端 resolver —— 一次 HTTP 拿服务端算好的首选线路，命中即用（也是「快速起播」
 *    的关键：不等本地全量匹配跑完）；
 *  - L2 本地 /vod/detail 全量匹配 —— 端侧实测 ~0.9s 能出 12 条线路，兜底主力；
 *  - L3 云端 resolver 的 backups —— 云端算了多条时，用作换线候选；
 *  - L4 跨源同名线路 —— catalog 里其它源命中同一部影片的线路；
 *  - L5 空 —— 真的没有线路，调用方弹「暂无片源」。
 *
 * **云端拿不到结果必须回落**：resolver 返回 TEMPORARILY_UNAVAILABLE / RESOLVING /
 * FALLBACK_SEARCHING 等非 MATCHED 状态时一律继续往下走，绝不能把「云端没匹配上」当成
 * 「这部片子没片源」——端侧匹配是好的（实测 12 条线路），让它回落才不至于把功能改坏。
 *
 * 抽成纯函数是为了能直接单测这条阶梯，而不必把仓库的 IO 全跑起来。
 */
internal object SourceResolveCascade {

    /** 每一级的来源标记，会写进埋点用于事后归因。 */
    enum class Tier(val wire: String) {
        LAST_GOOD("last_good"),
        CLOUD_PREFERRED("cloud_preferred"),
        LOCAL_DETAIL("local_detail"),
        CLOUD_BACKUP("cloud_backup"),
        CROSS_SOURCE("cross_source"),
        NONE("none"),
    }

    /**
     * 按优先级挑一条线路。
     *
     * @param lastGood 本地记忆的线路 id（L0）。
     * @param cloudPreferred 云端 resolver 的首选线路 id（L1）。
     * @param localDetailLines 本地 `/vod/detail` 匹配出的线路 id 列表（L2）。
     * @param cloudBackupLines 云端 resolver 的备用线路 id 列表（L3）。
     * @param crossSourceLines 跨源同名线路 id 列表（L4）。
     * @return 命中的线路 id 与来源级别；一个都没有时返回 [Tier.NONE] + null。
     */
    fun pick(
        lastGood: String? = null,
        cloudPreferred: String? = null,
        localDetailLines: List<String> = emptyList(),
        cloudBackupLines: List<String> = emptyList(),
        crossSourceLines: List<String> = emptyList(),
    ): Pair<String?, Tier> {
        lastGood?.takeIf { it.isNotBlank() }?.let { return it to Tier.LAST_GOOD }
        cloudPreferred?.takeIf { it.isNotBlank() }?.let { return it to Tier.CLOUD_PREFERRED }
        localDetailLines.firstOrNull { it.isNotBlank() }
            ?.let { return it to Tier.LOCAL_DETAIL }
        cloudBackupLines.firstOrNull { it.isNotBlank() }
            ?.let { return it to Tier.CLOUD_BACKUP }
        crossSourceLines.firstOrNull { it.isNotBlank() }
            ?.let { return it to Tier.CROSS_SOURCE }
        return null to Tier.NONE
    }

    /**
     * 云端 resolver 响应是否可以直接用。
     *
     * 只有拿到 preferred 引用才 true；其余状态（RESOLVING / TEMPORARILY_UNAVAILABLE /
     * FAST_PATH_FAILED / FALLBACK_SEARCHING）都要回落本地匹配。STALE_AVAILABLE 只要
     * 带着 preferred 就能用（过期索引也是一条能播的线路）。
     */
    fun cloudUsable(status: String, hasPreferred: Boolean): Boolean =
        hasPreferred && status in setOf("MATCHED", "STALE_AVAILABLE")
}

/**
 * 云端 resolver 的时序预算（2026-09-28）。
 *
 * 抽成独立可测对象的原因：这条不是「风格」而是**这次事故的根因**。原先
 * `pickPlayableSources` 串行地 `await` 云端，而它用的是与本地详情同一个
 * `detailClient`（readTimeout 45s）。自建 VPS 的出口一旦超时，「点播放 → 出画面」
 * 就被一个**可选提速路径**卡住 45s，而端侧本地匹配其实 ~0.9s 就能出 12 条线。
 *
 * 两个预算是同一个意思的两面：
 *  - [httpTimeoutSeconds] 给 resolver 的 HTTP 调用本身设上限（它读的是服务端索引，
 *    正常是毫秒级，3s 还回来说明这条路此刻不健康）；
 *  - [graceMs] 给本地线路先到之后的「插队」窗口——云端可能在几十毫秒内返回一条
 *    **更优**的 preferred，值得等一小会儿；但宽限一过就用本地起播。
 *
 * 合起来保证：**云端最多只比本地多花 [graceMs]，绝不让云端超时进入起播关键路径。**
 */
internal object SourceResolveTimingPolicy {

    /** resolver HTTP 读/连超时（秒）。刻意远小于本地详情的 45s。 */
    const val HTTP_TIMEOUT_SECONDS = 3L

    /** 本地线路到手后仍愿意为云端 preferred 额外等待的毫秒数。 */
    const val GRACE_MS = 1_500L

    /**
     * 自检：resolver 的预算必须显著小于本地详情匹配的上限。
     *
     * 这是把「别再把可选优化接回阻塞路径」写成一个会被测到的数字，而不是靠注释
 * 维持的约定——将来有人图省事把 [HTTP_TIMEOUT_SECONDS] 调回 45，这里立刻红。
     */
    val isBoundedAgainstDetailTimeout: Boolean get() = HTTP_TIMEOUT_SECONDS < 45L
}

internal data class CatalogSnapshot(
    val categories: List<Category>,
    val items: List<VodItem>,
)

/**
 * L0「上次成功线路」记忆的线路号编解码。
 *
 * 后端 `/vod/detail` 的线路 id 格式是 `{source_id}:{content_id}:{lineIndex}`，第三段
 * 是「该内容在源下的第几条线路」（见 `tools/backend-src/vod_sources.py`
 * `"%s:%s:%d" % (source.id, candidate["id"], index)`），**不是集号**。
 *
 * 2026-09-28 修的真实缺陷：L0 记忆在写入时只留 `source_id` + `content_id`、丢掉了
 * lineIndex，读回时又硬编码补 `:0`。于是「上次成功播的是第 3 条线路」被记成第 0 条，
 * 下次起播去播**另一条线路**——记忆反而变成了「随机换线」，比没有记忆更糟。
 *
 * 记��的是稳定标识（源/内容/线路号），**不含任何播放地址、Cookie 或签名 token**。
 */
internal object LastGoodSourceCodec {

    /** 解出的一条线路记忆。 */
    data class Stored(val sourceId: String, val contentId: String, val lineIndex: Int) {
        /** 还原成 `/vod/play` 用的 line id。 */
        fun lineId(): String = "$sourceId:$contentId:$lineIndex"
    }

    /**
     * 解析 line id；无效返回 null。
     *
     * 「无效就判无效」而不是「尽力猜一个」：记忆指向错误线路的代价（用户莫名其妙被
     * 切到另一条线）远大于没有记忆（多跑一轮本地匹配）。宁可回落 L2。
     */
    fun encode(rawLineId: String): Stored? {
        val raw = rawLineId.trim()
        if (raw.isEmpty()) return null
        val parts = raw.split(":")
        if (parts.size < 2 || parts.size > 3) return null
        val sourceId = parts[0].trim()
        val contentId = parts[1].trim()
        if (sourceId.isEmpty() || contentId.isEmpty()) return null
        // 两段 id（云端 resolver 候选）= 该内容在源下的第 0 条线路。
        // 三段 id 的第三段若不是非负整数，按 0 处理而不是丢弃整条记忆。
        val lineIndex = parts.getOrNull(2)
            ?.trim()
            ?.toIntOrNull()
            ?.takeIf { it >= 0 }
            ?: 0
        return Stored(sourceId, contentId, lineIndex)
    }
}

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
     * 云端 resolver 专用客户端（2026-09-28）。
     *
     * 刻意用**比 [detailClient] 短得多的**超时：resolver 是一个可选优化（它算的
     * preferred 比本地第一条更准），而 [detailClient] 的 45s 是给「本地详情匹配」
     * 留的（端侧实测冷路径要 12~30s）。用同一个 45s 客户端打 resolver，就是让一个
     * 可选分支具备和主路径一样长的阻塞能力——出口一慢，整条起播链就跟着慢。
     *
     * 3s 的含义：正常情况下 resolver 是毫秒级命中（读本地索引）；3s 还没回来，
     * 说明这条优化路径此刻不健康，本地线路早该顶上。
     */
    private val resolveClient by lazy {
        okHttp.newBuilder()
            .readTimeout(RESOLVE_CLIENT_TIMEOUT_SECONDS, java.util.concurrent.TimeUnit.SECONDS)
            .connectTimeout(3, java.util.concurrent.TimeUnit.SECONDS)
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
                // Search is about individual works: different releases/adaptations can share a
                // title (for example 红楼梦) and must remain separate results. Only collapse the
                // same catalogue row when it was duplicated by its stable ID.
                .distinctBy(VodItem::id)
        }
        return withContext(Dispatchers.Default) {
            searchLegacyCatalog(_state.value.catalog, q)
        }
    }

    private fun metadataCategories(items: List<VodItem>): List<Category> = items
        .distinctBy { it.categoryId }
        .map { it.categoryId.removePrefix("douban:") }
        .let(::metadataCategoriesForKeys)

    private fun metadataCategoriesForKeys(keys: List<String>): List<Category> {
        val uniqueKeys = keys.distinct()
        val preferredOrder = listOf("movie", "tv", "anime", "variety", "short")
        val orderedKeys = preferredOrder.filter { it in uniqueKeys } + uniqueKeys.filterNot { it in preferredOrder }
        return orderedKeys.map { key ->
            Category("douban:$key", when (key) {
                "movie" -> "电影"
                "tv" -> "电视剧"
                "anime" -> "国漫"
                "variety" -> "综艺"
                "short" -> "短剧"
                else -> key
            })
        }
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

    private val sectionFetchedAt = java.util.concurrent.ConcurrentHashMap<Int, Long>()

    suspend fun channelSections(categoryId: String, force: Boolean = false) {
        val key = categoryId.removePrefix("douban:")
        val cid = categoryNumber(key) ?: return
        val now = android.os.SystemClock.elapsedRealtime()
        if (!force && now - (sectionFetchedAt[cid] ?: -60_000L) < 60_000L &&
            _sections.value[cid].orEmpty().any { it.items.isNotEmpty() }) return
        val base = settings.current().hhkanBaseUrl
        val secs = withContext(Dispatchers.IO) {
            runCatching { VodApiSource.fetchHome(base, okHttp, key, refresh = force).sections }.getOrDefault(emptyList())
        }
        if (secs.any { it.items.isNotEmpty() }) {
            _sections.update { current -> current + (cid to secs) }
            sectionFetchedAt[cid] = android.os.SystemClock.elapsedRealtime()
        }
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
                ).distinctBy { it.localIdentityKey() }
                .map { VodApiSource.fromCatalogMedia(it, settings.current().hhkanBaseUrl) }
            if (localItems.isNotEmpty() && _home.value.sections.isEmpty()) {
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
                // 豆瓣有内容就替换对应本地栏，不把旧列表追加到新榜单。
                HomeState(
                    loading = false,
                    sections = preferDoubanSections(_home.value.sections, d.sections),
                    carousel = d.carousel,
                )
            } else {
                _home.value.copy(loading = false)
            }
            if (d?.stale == true && !force) {
                val refreshed = runCatching { VodApiSource.fetchHome(base, okHttp, "movie", refresh = true) }
                    .getOrNull()
                if (refreshed != null) {
                    _home.value = _home.value.copy(
                        sections = preferDoubanSections(_home.value.sections, refreshed.sections),
                        carousel = refreshed.carousel,
                    )
                }
            }
        }
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

    /** Local metadata source for fixed library sections, preserving each query's ranking. */
    private fun localMetadataBlocks(
        category: String,
        hotRows: List<CatalogMedia>,
        newRows: List<CatalogMedia>,
        updatedRows: List<CatalogMedia>,
        imageProxyBase: String,
    ): List<HSec> {
        fun inCategory(rows: List<CatalogMedia>) = rows
            .filter { media -> metadataCategoryKey(media.mediaType) == category }
            .distinctBy { it.localIdentityKey() }

        val hot = inCategory(hotRows)
        val newest = inCategory(newRows)
        val updated = inCategory(updatedRows).filter { it.updatedAt > 0L }
        if (hot.isEmpty() && newest.isEmpty() && updated.isEmpty()) return emptyList()

        fun section(name: String, selected: List<CatalogMedia>) = HSec(
            name,
            selected.take(24).map { VodApiSource.fromCatalogMedia(it, imageProxyBase) },
        )

        return listOf(
            section("最近热门", hot),
            section("最新上线", newest),
            section("最近更新", updated),
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
            if (libBlocksHaveContent(cid)) return true
            val waitMs = (deadline - System.currentTimeMillis()).coerceAtMost(LIB_CONTENT_POLL_MS)
            if (waitMs <= 0L) return false
            val appeared = withTimeoutOrNull(waitMs) {
                libBlocksFlow.first { libBlocksHaveContent(cid) }
            }
            if (appeared != null) return true
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
            val rankedRows = runCatching {
                Triple(
                    metadataCatalog.topRows(category, "hot"),
                    metadataCatalog.topRows(category, "new"),
                    metadataCatalog.topRows(category, "updated"),
                )
            }.getOrNull()
            val localSections = rankedRows?.let { (hot, newest, updated) ->
                localMetadataBlocks(category, hot, newest, updated, base)
            }.orEmpty()
            val hasLocalSections = localSections.any { it.items.isNotEmpty() }
            val installedMetadata = runCatching { metadataCatalog.hasInstalledDatabase() }.getOrDefault(false)
            if (!libBlocksHaveContent(cid)) {
                _libBlocks.value = _libBlocks.value + (cid to completeBlocks(localSections))
            }
            if (hasLocalSections) markHotSettled(cid)

            fun applyDouban(data: HhkanSource.HomeData) {
                val sections = data.sections.mapNotNull { section ->
                    when (section.name) {
                        "最近热门" -> section
                        "最新上映" -> section.copy(name = "最新上线")
                        else -> null
                    }
                }
                _libBlocks.update { current ->
                    current + (cid to completeBlocks(preferDoubanSections(current[cid].orEmpty(), sections)))
                }
                if (sections.any { it.items.isNotEmpty() }) markHotSettled(cid)
            }
            val (douban, onlineUpdated) = coroutineScope {
                val homeRequest = async {
                    if (base.isBlank()) null else runCatching {
                        VodApiSource.fetchHome(base, okHttp, category, refresh = force)
                    }.onFailure { android.util.Log.w("OTV", "分类 $cid 豆瓣目录拉取失败: ${it.message}") }.getOrNull()
                }
                val updatesRequest = async {
                    if (base.isBlank()) emptyList() else runCatching {
                        HhkanSource.fetchChannelSections(base, okHttp, cid)
                            .firstOrNull { it.name == "最近更新" }
                            ?.items
                            .orEmpty()
                            .take(24)
                    }.onFailure { android.util.Log.w("OTV", "分类 $cid 最近更新拉取失败: ${it.message}") }
                        .getOrDefault(emptyList()).also { updates ->
                            if (updates.isNotEmpty()) {
                                _libBlocks.update { current ->
                                    val sections = current[cid].orEmpty().filterNot { it.name == "最近更新" } + HSec("最近更新", updates)
                                    current + (cid to completeBlocks(sections))
                                }
                                markHotSettled(cid)
                            }
                        }
                }
                var home = homeRequest.await()
                home?.let(::applyDouban)
                if (home?.stale == true && !force) {
                    val refreshed = runCatching { VodApiSource.fetchHome(base, okHttp, category, refresh = true) }
                        .getOrNull()
                    if (refreshed != null) {
                        home = refreshed
                        applyDouban(refreshed)
                    }
                }
                val updates = updatesRequest.await()
                home to updates
            }
            val hasFreshDouban = douban?.let { data ->
                !data.stale && data.sections.any { it.name == "最近热门" && it.items.isNotEmpty() }
            } == true
            val hasOnlineUpdates = onlineUpdated.isNotEmpty()

            if (hasFreshDouban || (base.isBlank() && hasLocalSections && installedMetadata)) {
                libDone += cid
                libFailAt.remove(cid)
                libRetries.remove(cid)
            } else if (hasLocalSections) {
                // The packaged seed is an immediate offline fallback. Re-read after catalogue refresh
                // so an installed SQLite package replaces those seed rows without delaying first paint.
                if (base.isNotBlank()) libFailAt[cid] = System.currentTimeMillis()
                scheduleLibRetry(cid)
            } else {
                libFailAt[cid] = System.currentTimeMillis()
                scheduleLibRetry(cid)
            }
            if (!hasLocalSections) markHotSettled(cid)
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
            ?: vodId.takeIf { it.startsWith("hhkan:") }
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

        /**
         * 云端 resolver 的 HTTP 读超时（秒）。
         *
         * resolver 只是一条**可选**的提速路径（它算的 preferred 比本地第一条更准），
         * 本地 `/vod/detail` 匹配才是兜底主力。给它 45s（= detailClient）等于让一个
         * 可选优化具备和主路径同等的阻塞能力。3s 足够正常命中（读本地索引是毫秒级），
         * 超时就该让本地线路顶上，而不是让用户对着黑屏等。
         */
        const val RESOLVE_CLIENT_TIMEOUT_SECONDS = SourceResolveTimingPolicy.HTTP_TIMEOUT_SECONDS

        /**
         * 本地线路到手后，仍愿意为云端 preferred 额外等待的时长（毫秒）。
         *
         * 并发跑之后，本地线路先到；这一刻给云端一个很短的宽限期，让它有机会作为
         * **更优**的首选插队（服务端可能已经缓存好、只是比本地慢几十毫秒）。
         * 宽限一过就用本地起播——绝不因为一个可选优化让用户继续等。
         */
        const val RESOLVE_GRACE_MS = SourceResolveTimingPolicy.GRACE_MS
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
            ?: when {
                vodId.startsWith("douban:") -> com.qiubo.optimaltv.data.model.VodItem(
                    id = vodId, sourceId = "douban", title = "", categoryId = "",
                    detailRef = vodId.removePrefix("douban:"),
                )
                vodId.startsWith("hhkan:") -> com.qiubo.optimaltv.data.model.VodItem(
                    id = vodId, sourceId = "hhkan", title = "", categoryId = "",
                    detailRef = vodId,
                )
                else -> return@withContext null
            }
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

    // ---------- 起播取源阶梯（L0→L5，2026-09-28） ----------

    /**
     * 起播取源结果。[lines] 已按「可用性 × 顺序」排好，[preferredIndex] 指向该点就播的那条。
     */
    internal data class SourcePick(
        val lines: List<LineInfo>,
        val preferredIndex: Int,
        val tier: SourceResolveCascade.Tier,
    ) {
        val isEmpty: Boolean get() = lines.isEmpty() || preferredIndex !in lines.indices

        /** 已有一条可立刻 prepare 的线路（不等所有线路到齐）。 */
        val hasImmediate: Boolean get() = !isEmpty
    }

    /**
     * 按 [SourceResolveCascade] 阶梯取一组可播线路。
     *
     * 与 [resolveLines] 的区别：那个是「把本地匹配到的线路原样交给播放器」，这个是
     * **跨数据源**找一条最快能播的——本地记忆 → 云端 resolver → 本地全量匹配 → 云端备用
     * → 跨源同名。返回的线路已按「首选项在前」排序，播放器直接取 [SourcePick.preferredIndex]。
     *
     * 云端 resolver 拿不到结果时**必须**回落到本地 `/vod/detail`——端侧匹配是好的，
     * 把它当权威会让「云端暂时不可用」直接变成「用户看到空片源」。
     *
     * @param item 影片条目（[VodItem.mytvId] 为空时跳过云端 resolver，只走本地）。
     * @param epIndex 集下标（0 基）。
     * @param db last_good 记忆表；为 null 时不读 L0（便于单测与无 DB 场景）。
     */
    internal suspend fun pickPlayableSources(
        item: VodItem,
        epIndex: Int,
        lastGoodDao: com.qiubo.optimaltv.data.db.LastGoodSourceDao? = null,
    ): SourcePick = withContext(Dispatchers.IO) {
        val detailRef = item.detailRef.ifBlank { item.id.removePrefix("douban:") }
        val mytvId = item.mytvId.trim().toLongOrNull()

        // ---- L0 本地 last_good：微秒级 Room 读，上次成功过的线路最可信 ----
        val lastGoodLineId = if (mytvId != null && lastGoodDao != null) {
            runCatching { lastGoodDao.get(mytvId, epIndex) }
                .onFailure { android.util.Log.w("OTV", "last_good 读取失败: ${it.message}") }
                .getOrNull()
                ?.let {
                    // 还原成完整 line id：第三段是**线路号**，不能硬编码 0。
                    // 写坏成 0 会让「上次播第 3 条」变成「这次播第 0 条」——
                    // 记忆不仅失效，还会静默把用户换到另一条线路。
                    LastGoodSourceCodec.encode("${it.sourceId}:${it.contentId}:${it.lineIndex}")?.lineId()
                }
        } else {
            null
        }

        // ---- L1 云端 resolver 与 L2 本地全量匹配**并发**跑 ----
        //
        // 这里原来是串行的：先 await 云端再跑本地。而 detailClient 的 readTimeout 是 45s，
        // 2026-09-28 实测自建 VPS 的云端出口全部超时——于是「点播放」会先黑屏 45s，
        // 而端侧本地匹配其实 ~0.9s 就能出 12 条线。云端在这里是**优化**（它算的 preferred
        // 比本地第一条更准），不是**前提**；把它串在本地前面，等于让一个可选的优化
        // 变成整个起播的阻塞前提。
        //
        // 改成并发后：
        //  - 本地线路无论云端死活都能按时到手（云端超时不再拖起播）；
        //  - 云端仍然优先于本地（preferred 胜出，见 [SourceResolveCascade.pick]），
        //    所以服务端算好的最优线路没有白白浪费。
        val cloudDeferred = if (mytvId != null) {
            // 用一个独立的短超时客户端：本地结果不依赖它，但也不该让整个协程
            // 因为一个可选分支而无限期挂着。
            async {
                // 必须打服务端（Release 注入的 api 入口），不是本机内嵌后端：
                // 用 hhkanBaseUrl 时它默认是 127.0.0.1:8090，L1 云端预匹配永远
                // 打不到服务端，实测每次都回落 local_detail、逐条试错到 78.8s
                // 才出画。详见 CloudResolveBase 的说明。
                val base = CloudResolveBase.pick(settings.current())
                runCatching {
                    VodApiSource.fetchResolve(base, resolveClient, mytvId, epIndex + 1)
                }.onFailure {
                    android.util.Log.w("OTV", "resolve mytv=$mytvId ep=$epIndex 失败: ${it.message}")
                }.getOrNull()
            }
        } else {
            null
        }
        val localLines = runCatching { resolveLines(item, epIndex) }
            .onFailure { android.util.Log.w("OTV", "本地线路匹配失败: ${it.message}") }
            .getOrDefault(emptyList())

        // 本地出结果后，最多再给云端 RESOLVE_GRACE_MS 让它「插队」成为首选；
        // 超时不等——宁可拿本地第一条起播，也不把可选优化变成阻塞。
        val resolve = cloudDeferred?.let { d ->
            runCatching {
                withTimeoutOrNull(RESOLVE_GRACE_MS) { d.await() }
            }.getOrNull()
        }
        cloudDeferred?.let { d ->
            // 宽限期用尽就放弃等待。cancel() 只停掉协程的「结果交付」，不会中断已经
            // 阻塞在 socket 上的 execute()——那一次请求最坏跑满 resolveClient 的 3s
            // 读超时后自行结束。这仍然远好于旧实现的 45s：用户早已用本地线路起播，
            // 残留的那次请求只占一个 IO 线程，不在关键路径上。
            if (!d.isCompleted) d.cancel()
        }
        val cloudUsable = resolve != null &&
            SourceResolveCascade.cloudUsable(resolve.status, resolve.preferred != null)
        if (resolve != null && !cloudUsable) {
            android.util.Log.i(
                "OTV",
                "resolver status=${resolve.status} 无可用线路 → 回落本地 /vod/detail 匹配",
            )
        }

        // ---- L3 云端备用线路 ----
        val cloudBackupIds = resolve?.backups.orEmpty().map { it.lineId() }

        // ---- L4 跨源同名线路 ----
        val crossSourceIds = siblings(item.normTitle)
            .filter { it.id != item.id }
            .filter { epIndex < it.episodes.size }
            .map { it.detailRef.ifBlank { it.id } }

        // ---- 按阶梯挑一条，并把所有候选线路并进可换线列表 ----
        val localIds = localLines.map { it.playRef.substringBefore('|').ifBlank { it.vodId } }
        val (pickedId, tier) = SourceResolveCascade.pick(
            lastGood = lastGoodLineId,
            cloudPreferred = resolve?.takeIf { cloudUsable }?.preferred?.lineId(),
            localDetailLines = localIds,
            cloudBackupLines = cloudBackupIds,
            crossSourceLines = crossSourceIds,
        )

        if (pickedId == null) {
            return@withContext SourcePick(localLines, 0, SourceResolveCascade.Tier.NONE)
        }

        // 把阶梯选中的线路提到列表最前：播放器按 index 起播，用户换线时其余线路仍可用。
        val reordered = reorderWithPreferred(localLines, pickedId, detailRef)
        val index = reordered.indexOfFirst { it.playRef.substringBefore('|') == pickedId }
            .let { if (it >= 0) it else reordered.indexOfFirst { it.vodId == pickedId } }
            .coerceAtLeast(0)
        android.util.Log.i("OTV", "取源 tier=$tier mytv=$mytvId ep=$epIndex → 第 ${index + 1}/${reordered.size} 条")
        SourcePick(reordered, index, tier)
    }

    /**
     * 把 [preferredId] 对应的线路提到列表首位。
     *
     * 命中本地线路列表时直接换序；命中不了（例如只存在于 last_good / 云端引用）时，
     * 用 `{detailRef}|{lineId}|{epIndex}` 拼一条新线路——`/vod/play` 只按 line id 定位，
     * 拼出来的引用照样能解析出直链。
     */
    private fun reorderWithPreferred(
        lines: List<LineInfo>,
        preferredId: String,
        detailRef: String,
    ): List<LineInfo> {
        if (lines.isEmpty()) return lines
        val hit = lines.indexOfFirst { it.playRef.substringBefore('|') == preferredId }
        if (hit <= 0) {
            if (hit < 0) {
                val synthetic = LineInfo(
                    vodId = lines.first().vodId,
                    label = "线路·${preferredId.substringBefore(':')}",
                    url = "",
                    playRef = "$detailRef|$preferredId|0",
                )
                return listOf(synthetic) + lines
            }
            return lines
        }
        return lines.toMutableList().also { it.add(0, it.removeAt(hit)) }
    }

    /**
     * 起播成功后记住这条线路（写 last_good）。
     *
     * 只存 `{mytv_id, episode_index, source_id, content_id, line_index}`——**不存播放地址、
     * Cookie 或任何签名 token**（见 [com.qiubo.optimaltv.data.db.LastGoodSourceEntity]）。
     * 写入失败绝不影响播放，永远静默。
     *
     * [lineIndex] 必须原样保留：它是「该内容在源下的第几条线路」。丢掉它（写 0）会让
     * 下次起播去播另一条线路——记忆从「更快」变成「乱换」。
     */
    internal suspend fun rememberLastGoodSource(
        mytvId: Long,
        epIndex: Int,
        lineId: String,
        dao: com.qiubo.optimaltv.data.db.LastGoodSourceDao? = null,
    ) {
        val target = dao ?: return
        val stored = LastGoodSourceCodec.encode(lineId) ?: return
        runCatching {
            target.upsert(
                com.qiubo.optimaltv.data.db.LastGoodSourceEntity(
                    mytvId = mytvId,
                    episodeIndex = epIndex,
                    sourceId = stored.sourceId,
                    contentId = stored.contentId,
                    lineIndex = stored.lineIndex,
                    successfulAt = System.currentTimeMillis(),
                ),
            )
        }.onFailure { android.util.Log.w("OTV", "last_good 写入失败: ${it.message}") }
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

package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.data.model.Category
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
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

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
    const val SNAPSHOT_VERSION = 3

    fun allows(sourceId: String, itemId: String): Boolean =
        sourceId == "douban" && itemId.startsWith("douban:")
}

internal object VodIdPolicy {
    fun isCatalogId(id: String): Boolean = id.startsWith("douban:")
}

internal object PlaybackLineSelector {
    fun nextAfterFailure(rankedIds: List<String>, activeId: String?, failed: Set<String>): String? {
        val start = rankedIds.indexOf(activeId).coerceAtLeast(-1) + 1
        return rankedIds.drop(start).firstOrNull { it !in failed }
    }
}

private data class CatalogSnapshot(
    val categories: List<Category>,
    val items: List<VodItem>,
)

/** 目录连续空结果时的后台重试间隔（毫秒）：内嵌后端冷启动/热更重启需要的时间窗。 */
private val CATALOG_RETRY_DELAYS = longArrayOf(5_000L, 10_000L, 20_000L, 30_000L)

/** 内容仓库（技术方案 §3.1 业务层·内容仓库）：好好看目录 / 分类 / 搜索。 */
class VodRepository(
    private val settings: SettingsStore,
    private val okHttp: okhttp3.OkHttpClient,
    private val appContext: android.content.Context,
) {
    private val _state = MutableStateFlow(CatalogState(loading = true))
    val state: StateFlow<CatalogState> = _state.asStateFlow()

    /** 详情专用客户端：豆瓣详情要做「多线路测活 + 清晰度/延时综合排序」，冷路径实测
     *  15～20s（RemoteApiSource.defaultClient 的 10s 读超时会让它整条失败 → 详情页
     *  「暂无片源/暂无简介」）；这里放宽到 45s，与 HhkanSource 的详情口径一致。 */
    private val detailClient by lazy {
        okHttp.newBuilder().readTimeout(45, java.util.concurrent.TimeUnit.SECONDS).build()
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
        // 内置后端就绪探测：app 内置 proxy 冷启动需几秒，先轮询 /api/health（仅当指向 127.0.0.1）。
        // 2026-09-18：预算 15s → 40s。热更刚落地的第一次启动要重解压 38MB 包 + Chaquopy 起
        // Python + 装载 22MB 快照，15s 常常刚好不够；探测本身是本地回环，超预算即放弃。
        waitBackendReady(cfg.hhkanBaseUrl, tries = 80)
        // 目录和元数据只来自设备端豆瓣模块；播放源只在详情/播放阶段参与匹配。
        val sources = buildList {
            if (cfg.hhkanBaseUrl.isNotBlank()) add(VodApiSource(cfg.hhkanBaseUrl, okHttp))
        }
        try {
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

    fun search(query: String): List<VodItem> {
        val q = query.trim()
        if (q.isEmpty()) return emptyList()
        return _state.value.catalog?.dedupedItems?.filter { item ->
            item.title.contains(q, true) || item.tags.any { it.contains(q, true) }
                    || item.year == q || item.area.contains(q, true)
        } ?: emptyList()
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
                HomeState(loading = false, sections = d.sections, carousel = d.carousel)
            } else {
                _home.value.copy(loading = false)
            }
        }
    }

    /** 等待进行中的首页拉取结束（libBlocks「最近热门」块依赖 home sections） */
    suspend fun awaitHome() {
        refreshHome()
        homeJob?.join()
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
    /** 后端先回过期快照时，等待同一请求的后台 SWR 完成，再把三栏替换成当天数据。 */
    private val staleLibRefreshJobs = java.util.concurrent.ConcurrentHashMap<Int, kotlinx.coroutines.Job>()

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

    /** 三块展示顺序与网页、后端一致；空栏目也保留，防止标题被吞。 */
    private val blockOrder = listOf("最近热门", "最新上映", "豆瓣高分")
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

    /**
     * 拉分类下三块：先取好好看频道页自带的当前分类三栏目；只有某栏缺失时才按
     * 当前分类补位。旧版先用首页/全站 latest 拼接，既会混类，也会让三栏接口沦为
     * 全挂后的兜底，源站稍有波动便丢标题。
     */
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
            if (base.isBlank()) {
                _libBlocks.value = _libBlocks.value + (cid to completeBlocks(emptyList()))
                markHotSettled(cid)
                return@launch
            }
            fun store(sections: List<HSec>) {
                _libBlocks.value = _libBlocks.value + (cid to completeBlocks(sections))
            }
            val home = runCatching { VodApiSource.fetchHome(base, okHttp, category, refresh = force) }
                .onFailure { android.util.Log.w("OTV", "豆瓣分类三栏失败: ${it.message}") }
                .getOrNull()
            val doubanSections = home?.sections.orEmpty()
            store(doubanSections)
            if (home?.stale == true) {
                refreshStaleHome(cid, category)
                libFailAt.remove(cid)
                libRetries.remove(cid)
            } else if (doubanSections.any { it.items.isNotEmpty() }) {
                libDone += cid
                libFailAt.remove(cid)
                libRetries.remove(cid)
            } else {
                libFailAt[cid] = System.currentTimeMillis()
                scheduleLibRetry(cid)
            }
            markHotSettled(cid)
        }
        // 超时兜底：慢网 8s 后先展示固定三栏空态；后到内容只填栏，不改信息架构。
        repoScope.launch {
            kotlinx.coroutines.delay(8_000L)
            markHotSettled(cid)
        }
    }

    /**
     * /vod/home 的 stale-while-revalidate 首次会先给随包快照。
     * 快照不能算作当天刷新完成：后台抓取结束后再次取同一接口，收到 fresh 数据就替换三栏。
     */
    private fun refreshStaleHome(cid: Int, category: String) {
        if (staleLibRefreshJobs[cid]?.isActive == true) return
        staleLibRefreshJobs[cid] = repoScope.launch {
            try {
                repeat(30) {
                    kotlinx.coroutines.delay(1_000L)
                    val home = runCatching {
                        VodApiSource.fetchHome(settings.current().hhkanBaseUrl, okHttp, category)
                    }.getOrNull() ?: return@repeat
                    if (home.stale) return@repeat
                    if (home.sections.any { it.items.isNotEmpty() }) {
                        _libBlocks.value = _libBlocks.value + (cid to completeBlocks(home.sections))
                        libDone += cid
                        libFailAt.remove(cid)
                        libRetries.remove(cid)
                    }
                    return@launch
                }
            } finally {
                staleLibRefreshJobs.remove(cid)
            }
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
            fetchDetailCached(item.detailRef)?.meta?.takeIf { it.isNotBlank() } ?: item.year
        } else {
            (listOf(item.year, item.area).filter { it.isNotBlank() } + item.tags).joinToString(" / ")
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
        val item = itemById(vodId) ?: if (vodId.startsWith("douban:")) {
            com.qiubo.optimaltv.data.model.VodItem(
                id = vodId, sourceId = "douban", title = "", categoryId = "",
                detailRef = vodId.removePrefix("douban:"),
            )
        } else return@withContext null
        if (item.detailRef.isBlank()) return@withContext item
        val d = fetchDetailCached(item.detailRef, forceRefresh) ?: return@withContext cached
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

    private fun readSnapshotCandidates(): List<Pair<String, String>> = buildList {
        val f = snapshotFile
        if (f.exists() && f.length() > 0L) {
            runCatching { f.readText() }.getOrNull()?.let { add("磁盘快照" to it) }
        }
        // 磁盘快照过旧、损坏或混入补充源时也必须回退到可信种子，而不是空屏。
        runCatching {
            appContext.assets.open("catalog_seed.json").use { it.readBytes().toString(Charsets.UTF_8) }
        }.getOrNull()?.let { add("内置种子" to it) }
    }

    private fun decodeSnapshot(text: String): CatalogSnapshot? {
        val o = org.json.JSONObject(text)
        if (o.optInt("v") != CatalogOriginPolicy.SNAPSHOT_VERSION) return null
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
            ).takeIf { it.id.isNotBlank() && it.title.isNotBlank() }
        }
        if (cats.isEmpty() || items.isEmpty()) return null
        if (items.any { !CatalogOriginPolicy.allows(it.sourceId, it.id) }) return null
        return CatalogSnapshot(cats, items)
    }

    private fun restoreSnapshot() {
        var restored: Pair<String, CatalogSnapshot>? = null
        for ((label, text) in readSnapshotCandidates()) {
            val decoded = runCatching { decodeSnapshot(text) }.getOrNull()
            if (decoded != null) {
                restored = label to decoded
                break
            }
            android.util.Log.w("OTV", "$label 已损坏或含非好好看卡片，跳过")
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
        o.put(
            "items",
            org.json.JSONArray().apply {
                items.forEach {
                    put(
                        org.json.JSONObject()
                            .put("id", it.id).put("s", it.sourceId).put("t", it.title)
                            .put("c", it.categoryId).put("y", it.year).put("ar", it.area)
                            .put("r", it.rating).put("p", it.posterUrl).put("h", it.heroUrl)
                            .put("rm", it.remark).put("d", it.detailRef),
                    )
                }
            },
        )
        // 临时文件 + 原子改名：写一半崩溃/断电不会留下半个快照
        val tmp = java.io.File(snapshotFile.parentFile, snapshotFile.name + ".tmp")
        tmp.writeText(o.toString())
        if (!tmp.renameTo(snapshotFile)) {
            snapshotFile.delete()
            tmp.renameTo(snapshotFile)
        }
        android.util.Log.i("OTV", "目录快照落盘：${items.size} 条")
    }
}

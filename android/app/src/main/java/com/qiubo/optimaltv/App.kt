package com.qiubo.optimaltv

import android.content.Context
import coil.ImageLoader
import coil.ImageLoaderFactory
import coil.disk.DiskCache
import coil.memory.MemoryCache
import com.qiubo.optimaltv.data.db.AppDb
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.data.repo.IptvRepository
import com.qiubo.optimaltv.data.repo.LiveRepository
import com.qiubo.optimaltv.data.repo.VodRepository
import com.qiubo.optimaltv.data.repo.heroRefs
import com.qiubo.optimaltv.data.source.RemoteApiSource
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.joinAll

/** 轻量服务定位器：单进程单实例（方案 §5 单 Activity 单进程） */
object Graph {
    lateinit var appContext: Context
        private set
    lateinit var db: AppDb
        private set
    lateinit var settings: SettingsStore
        private set
    lateinit var repo: VodRepository
        private set
    lateinit var live: LiveRepository
        private set
    lateinit var iptv: IptvRepository
        private set

    fun init(context: Context) {
        if (::appContext.isInitialized) return
        appContext = context.applicationContext
        db = AppDb.get(appContext)
        settings = SettingsStore(appContext)
        repo = VodRepository(settings, RemoteApiSource.defaultClient(), appContext)
        live = LiveRepository(settings, RemoteApiSource.defaultClient(), appContext)
        // v1.13 电视直播：公开 IPTV m3u 源仓库（init 即读磁盘快照，拉取由电视页触发）
        iptv = IptvRepository(appContext)
    }

    /** 应用级协程作用域（v1.17：偏好写入等跨页面生命周期任务，页面退出不丢写） */
    val appScope: kotlinx.coroutines.CoroutineScope by lazy {
        kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.SupervisorJob() + kotlinx.coroutines.Dispatchers.IO)
    }

    /** 图片双缓存（需求 影视#1：海报墙加载提速）——
     *  内存 20% + 磁盘 256MB：海报墙反复滚动/回页全部命中本地，不再重复走后端代理；
     *  读超时 45s：football-data 队标 SVG（~300KB）服务器很慢，默认 10s 会稳定超时
     *  导致英超等球队队标全部回退字母占位（实测根因）。 */
    fun imageLoader(): ImageLoader = ImageLoader.Builder(appContext)
        .components {
            // 队标 SVG（football-data crests）解码支持
            add(coil.decode.SvgDecoder.Factory())
        }
        .okHttpClient {
            okhttp3.OkHttpClient.Builder()
                .connectTimeout(10, java.util.concurrent.TimeUnit.SECONDS)
                .readTimeout(45, java.util.concurrent.TimeUnit.SECONDS)
                // 需求 影视#1：封面全部经 127.0.0.1:8090 后端中继，OkHttp 默认每主机仅 5 并发
                // → 海报墙十几张排队分 3 波加载，观感「很慢」；提到 32 并发一波全出
                .dispatcher(okhttp3.Dispatcher().apply {
                    maxRequests = 128
                    maxRequestsPerHost = 32
                })
                .build()
        }
        .memoryCache {
            MemoryCache.Builder(appContext).maxSizePercent(0.20).build()
        }
        .diskCache {
            DiskCache.Builder()
                .directory(appContext.cacheDir.resolve("image_cache"))
                .maxSizeBytes(256L * 1024 * 1024)
                .build()
        }
        // v1.10（2026-08-31 低端机优化）：硬件位图默认放开（海报渲染走 GPU 纹理，
        // 滚动/切页不再逐帧 CPU 拷贝）——仅 hero 超宽大图（3360×1080，MuMu 硬件位图
        // 解码失败=图空白，v1.4 实测）按请求 allowHardware(false)，见两处 HeroBand。
        .crossfade(true)
        .build()
}

/**
 * 启动门闸（2026-09-19 用户拍板「方案 A+B」：首屏快优先，推翻 v1.32 全量预热门闸）：
 *
 * - v1.32~2026-09-18：「全量预热后再放行」——足球/IPTV/目录/影视三栏/封面，直至
 *   hero 前 6 条的片源匹配（单条 12~30s、预算 60s）全塞进启动门闸，进 app 30~90s，
 *   是「进入 app 的加载界面很慢」的主根因。
 * - 现行口径（方案 A+B，用户确认接受「秒点 hero 第一条可能等片源」的取舍）：
 *   门闸只等「决定落点 + 首屏可见内容」——授权状态（决定落激活页还是主页）、
 *   IPTV 磁盘快照、目录快照、后端就绪 + home + hero 简介（0.4s 快路径）+ hero 海报；
 *   足球赛程/IPTV 拉新/目录刷新/影视三栏/全量封面/首屏片源匹配全部转后台补，
 *   片源预热结果仍落 detailCache/detailInfoCache，用户浏览几秒后点开大概率已命中，
 *   未命中也有详情页「正在查找片源」等待态兜底。
 * - [FULL_GATE_MS]（TV 90s / 移动 30s）降级为兜底上限：正常路径几秒内放行，
 *   只有弱网/异常才走到上限；主线程看门狗（上限 + 2s 强制放行）保留。
 */
object AppStartup {
    private val _ready = kotlinx.coroutines.flow.MutableStateFlow(false)
    val ready: kotlinx.coroutines.flow.StateFlow<Boolean> = _ready.asStateFlow()

    private val _status = kotlinx.coroutines.flow.MutableStateFlow("正在准备…")

    /**
     * 启动屏文案（2026-09-17 用户需求：全量预热期间人会盯着启动屏看，需要知道不是在卡死）。
     * 只作提示，不参与门控判定。
     */
    val status: kotlinx.coroutines.flow.StateFlow<String> = _status.asStateFlow()

    /** 本地三件套（授权/IPTV 快照/目录快照）预算：本地资源秒出，5s 是弱机/首装硬上限 */
    private const val FAST_GATE_MS = 5_000L

    /** 门闸兜底上限（方案 A+B 后正常路径几秒即放行，这只是弱网/异常的保险丝） */
    private val FULL_GATE_MS = com.qiubo.optimaltv.BuildConfig.STARTUP_GATE_MS

    /** hero 海报等待预算：门闸要「首屏有图才放行」，但弱网不能让启动屏无限等。 */
    private const val COVER_WAIT_MS = 20_000L

    /** 后台片源预热的 hero 条数：与 hero 简介预载同批（首屏最大、最可能被点到的候选） */
    private const val SOURCE_HERO_MAX = 6

    /** 后台片源预热的额外候选条数（首屏卡片行：最近观看 / 电影三栏首条） */
    private const val SOURCE_EXTRA_MAX = 6

    /** 后台片源预热并发；额外候选再压低一档避免与用户的点击抢上游 */
    private const val SOURCE_CONCURRENCY = 3
    private const val SOURCE_EXTRA_CONCURRENCY = 2

    fun begin() {
        val scope = kotlinx.coroutines.CoroutineScope(SupervisorJob() + Dispatchers.IO)
        val t0 = System.currentTimeMillis()
        _status.value = "正在准备…"
        // 后台全量预热（方案 A：不挡门闸）：足球赛程 / IPTV 拉新 / 目录刷新 /
        // 影视三栏+全量封面 / 首屏片源匹配。各自失败记日志，进页后按需兜底。
        scope.launch {
            launch {
                runCatching { Graph.live.matches() }
                    .onFailure { OtvLog.w("后台预热：比赛列表失败（后台重试）: ${it.message}") }
            }
            launch {
                runCatching { Graph.iptv.refreshIfStale() }
                    .onFailure { OtvLog.w("后台预热：电视源拉新失败（后台重试）: ${it.message}") }
            }
            launch {
                runCatching { Graph.repo.refresh() }
                    .onFailure { OtvLog.w("后台预热：目录刷新失败（后台重试）: ${it.message}") }
            }
            launch {
                runCatching { preloadDeep() }
                    .onFailure { OtvLog.w("后台深预热失败（进页后按需拉取）: ${it.message}") }
            }
        }
        scope.launch {
            try {
                val settled = kotlinx.coroutines.withTimeoutOrNull(FULL_GATE_MS) {
                    _status.value = "正在连接服务器…"
                    // 授权状态（门控判定依赖；DataStore 本地读，亚秒级）
                    val lic = if (com.qiubo.optimaltv.BuildConfig.LICENSE_ENABLED) {
                        launch {
                            kotlinx.coroutines.withTimeoutOrNull(4_000) {
                                com.qiubo.optimaltv.license.LicenseManager.state.first { it != null }
                            }
                        }
                    } else null
                    // IPTV 频道表：init 已读磁盘快照，这里只等它上屏（拉新转后台）
                    val iptv = launch {
                        var guard = 0
                        while (Graph.iptv.state.value.channels.isEmpty() && guard++ < 40) delay(100)
                    }
                    // 目录：快照恢复（磁盘/assets 种子）即就绪；首装无快照不死等网络
                    val cat = launch {
                        kotlinx.coroutines.withTimeoutOrNull(FAST_GATE_MS - 500) {
                            Graph.repo.awaitCatalogAvailable()
                        }
                    }
                    // 首屏链：后端就绪 → home → hero 简介 + hero 海报（方案 B 分档：只等这一档）
                    val firstScreen = launch {
                        runCatching { preloadFirstScreen() }
                            .onFailure { OtvLog.w("首屏预载失败（进页后按需拉取）: ${it.message}") }
                    }
                    _status.value = "正在加载首屏…"
                    listOfNotNull(lic, iptv, cat, firstScreen).joinAll()
                    _status.value = "即将进入…"
                    true
                }
                if (settled == null) {
                    OtvLog.w("startup gate 到底保上限（${FULL_GATE_MS}ms）仍未就绪 → 放行，剩余转后台")
                }
            } catch (e: Exception) {
                OtvLog.e("startup gate 异常（兜底放行）: ${e.message}")
            } finally {
                OtvLog.i("startup gate 放行 ${System.currentTimeMillis() - t0}ms" +
                    "（iptv=${Graph.iptv.state.value.channels.size} 频道, catalog=${Graph.repo.state.value.catalog != null}）")
                _ready.value = true
            }
        }
        // v1.19 硬保险：主线程 Handler 看门狗——门闸协程级超时（withTimeout）依赖
        // IO 线程池可调度；若任何同步阻塞把 IO 池占满（实测：Semaphore.acquire 死锁
        // 曾让兜底失效、启动屏永久冻结），协程超时永远恢复不了。本看门狗跑在
        // 主线程 Looper 上，不受影响：任何情况下启动屏最多「预算上限 + 2s」必放行。
        android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
            if (!_ready.value) {
                OtvLog.e("startup gate 看门狗触发：${FULL_GATE_MS + 2_000}ms 仍未放行 → 强制放行（协程链疑似饿死）")
                _ready.value = true
            }
        }, FULL_GATE_MS + 2_000)
    }

    /**
     * 首屏预载（方案 B 分档：门闸只等这一档）：后端就绪 → home 首抓 →
     * hero 简介（豆瓣快路径，实测 0.4s）并行预取 → hero 海报真实加载。
     * 三栏/全量封面/片源匹配在 [preloadDeep] 后台跑，不在这里等。
     */
    private suspend fun preloadFirstScreen() {
        val t0 = System.currentTimeMillis()
        // 内置后端（127.0.0.1:8090）冷启动需数秒：先等就绪再开抓，否则 home/hero 简介
        // 全部 ConnectException 秒挂（首启实测踩过）
        Graph.repo.awaitBackendReady()
        Graph.repo.refreshHome()
        Graph.repo.awaitHome()
        val heroIds = heroIds()
        // hero 简介（豆瓣 defer 快路径）按 UI 同一套 hero 口径（heroRefs）预取前 6 条，
        // 结果落 VodRepository.briefCache，进页同步命中直接出简介。
        kotlinx.coroutines.coroutineScope {
            heroIds.map { id -> launch { runCatching { Graph.repo.vodBrief(id) } } }.joinAll()
        }
        // hero 海报（carousel backdrop）真实加载并等待——「首屏有图才放行」；
        // 卡片封面量大，放 preloadDeep 后台补。heroCount=全部 → 保持整屏尺寸解码。
        val backdrops = Graph.repo.homeFlow.value.carousel
            .map { it.backdrop }.filter { it.isNotBlank() }
        val n = preloadCovers(backdrops, heroCount = backdrops.size)
        OtvLog.i("首屏预载完成：hero ${heroIds.size} 条简介 / $n 张海报（${System.currentTimeMillis() - t0}ms）")
    }

    /** 门闸与后台片源预热共用同一批 hero id（与 UI 同一套 heroRefs 口径） */
    private fun heroIds(): List<String> = runCatching {
        heroRefs(Graph.repo.state.value.catalog, Graph.repo.homeFlow.value).map { it.id }.distinct()
    }.getOrDefault(emptyList()).take(SOURCE_HERO_MAX)

    /**
     * 后台深预热（方案 A：不挡门闸）：全部分类三栏 + 全量封面 + 首屏片源匹配。
     * 片源预热结果落 detailCache/detailInfoCache：用户浏览几秒后点开详情页大概率
     * 已命中「立即播放」，未命中也有「正在查找片源」等待态兜底。
     */
    private suspend fun preloadDeep() {
        val t0 = System.currentTimeMillis()
        Graph.repo.awaitBackendReady()
        Graph.repo.awaitCatalogAvailable()
        val cats = Graph.repo.state.value.catalog?.categories ?: return
        cats.forEach { Graph.repo.libBlocks(it.id) }       // 全部分类三块并行拉取（幂等）
        val heroIds = heroIds()
        kotlinx.coroutines.coroutineScope {
            val blocksReady = cats.map { async { Graph.repo.awaitLibBlocksContent(it.id) } }.awaitAll()
            if (blocksReady.any { !it }) {
                OtvLog.w("后台深预热：${blocksReady.count { !it }}/${blocksReady.size} 个分类三栏仍为空（后台阶梯补抓）")
            }
        }
        val blocks = Graph.repo.libBlocksFlow.value
        val home = Graph.repo.homeFlow.value
        // URL 收集优先用刷新完成后的最新目录（refresh 与预载并行，通常此时已完成）
        val catsForUrls = Graph.repo.state.value.catalog?.categories ?: cats
        val urls = mutableListOf<String>()
        // hero 降级链三级图源：carousel 大图 → 首页热门板块封面 → 目录前 6 封面
        home.carousel.forEach { if (it.backdrop.isNotBlank()) urls += it.backdrop }
        home.sections.forEach { sec -> sec.items.forEach { if (it.posterUrl.isNotBlank()) urls += it.posterUrl } }
        Graph.repo.state.value.catalog?.dedupedItems?.filter { it.posterUrl.isNotBlank() }?.take(6)
            ?.forEach { urls += it.posterUrl }
        // 每个分类 tab 页（含「全部」兜底网格）：三块卡片封面；无三块的分类取目录前 18 张
        (catsForUrls.map { it.id } + "").forEach { cid ->
            val secs = com.qiubo.optimaltv.data.repo.VodRepository.categoryCid(cid)?.let { blocks[it] }.orEmpty()
            if (secs.isNotEmpty()) {
                secs.forEach { sec -> sec.items.forEach { if (it.posterUrl.isNotBlank()) urls += it.posterUrl } }
            } else {
                Graph.repo.byCategory(cid).take(18).forEach { if (it.posterUrl.isNotBlank()) urls += it.posterUrl }
            }
        }
        // 最近观看行封面（本地 DB，≤12 张）
        runCatching {
            Graph.db.vodDao().historyFlow(12).first().forEach { if (it.posterUrl.isNotBlank()) urls += it.posterUrl }
        }
        // 封面预载与首屏片源预热并行：前者打图床、后者打影视源，互不抢上游
        kotlinx.coroutines.coroutineScope {
            val srcJob = launch { preloadFirstScreenSources(heroIds) }
            val n = preloadCovers(urls.distinct(), heroCount = home.carousel.size)
            OtvLog.i("后台深预热完成：${catsForUrls.size} 分类 / $n 张封面（${System.currentTimeMillis() - t0}ms）")
            srcJob.join()
        }
    }

    /**
     * 首屏片源预热（方案 A 后全程后台，不再进启动门闸）：
     * hero 前 [SOURCE_HERO_MAX] 条 + 首屏卡片行候选（最近观看 / 电影三栏首条），
     * 多源搜索 + 线路探测结果落 VodRepository 的 detailCache/detailInfoCache。
     */
    private suspend fun preloadFirstScreenSources(heroIds: List<String>) {
        if (heroIds.isEmpty()) return
        val t0 = System.currentTimeMillis()
        runCatching { Graph.repo.preloadDetailSources(heroIds, batch = SOURCE_CONCURRENCY) }
            .onSuccess { OtvLog.i("首屏片源预热（后台）：hero $it/${heroIds.size} 条线路就绪（${System.currentTimeMillis() - t0}ms）") }
            .onFailure { OtvLog.w("首屏片源预热失败（点开时按需拉取）: ${it.message}") }
        // 首屏卡片行：最近观看 + 电影三栏每栏首条
        val extras = runCatching { firstScreenExtraIds() }.getOrDefault(emptyList())
            .filterNot { it in heroIds }.take(SOURCE_EXTRA_MAX)
        if (extras.isNotEmpty()) {
            val n = runCatching { Graph.repo.preloadDetailSources(extras, batch = SOURCE_EXTRA_CONCURRENCY) }.getOrDefault(0)
            OtvLog.i("首屏片源预热（后台）：卡片行 $n/${extras.size} 条线路就绪")
        }
    }

    /** 放行后继续预热的「首屏卡片行」候选：最近观看前 3 + 电影三栏每栏首条（去重后取前几条） */
    private suspend fun firstScreenExtraIds(): List<String> {
        val ids = mutableListOf<String>()
        runCatching {
            Graph.db.vodDao().historyFlow(3).first()
                .forEach { if (it.vodId.isNotBlank()) ids += it.vodId }
        }
        runCatching {
            Graph.repo.libBlocksFlow.value[1].orEmpty().forEach { sec ->
                sec.items.firstOrNull()?.id?.takeIf { it.isNotBlank() }?.let { ids += it }
            }
        }
        return ids.distinct()
    }

    /**
     * Coil 封面预热：走 App 的单例 ImageLoader（带内存+磁盘缓存），返回预载发起张数。
     * v1.19 流畅度（门闸期间启动屏转圈冻结的降载）：卡片封面按显示尺寸 2x（464×704）
     * 下采样解码——旧版无 size 默认按整屏 ~1920 解码，单张解码像素量是所需的 ~4 倍。
     * 2026-09-11（需求⑯ 海报提速）：旧版 24 张分批「批内全完成再发下一批」——一批里
     * 单张慢图（冷上游 ~1.1s+）拖住整批，前 24 张之后的海报要等最慢一张；改一次性
     * enqueue 全部 96 张，由 OkHttp dispatcher（32/host）天然限流在途请求——enqueue
     * 非阻塞、零线程占用，行为与 v1.19 教训（Semaphore 死锁）不冲突，首屏出图更早。
     * 卡片封面仍按 2x 下采样；hero 大图保持默认整屏尺寸。
     */
    private suspend fun preloadCovers(urls: List<String>, heroCount: Int): Int {
        if (urls.isEmpty()) return 0
        val loader = coil.Coil.imageLoader(Graph.appContext)
        val cappedUrls = urls.take(96)
        if (cappedUrls.size < urls.size) {
            OtvLog.i("封面预载收敛：${cappedUrls.size}/${urls.size} 张（其余进页按需加载）")
        }
        /* 2026-09-17 用户要求「影视界面要加载完成、有图片和信息后才能进入」：旧实现只把请求
           enqueue 出去就返回，门闸其实没等图，进页仍会一张张补。改成真正加载并等待完成。
           仍然限时：预算是 COVER_WAIT_MS，超时的图留在独立 scope 里跑完（不随门闸协程取消）。 */
        val pending = java.util.concurrent.atomic.AtomicInteger(cappedUrls.size)
        val allDone = kotlinx.coroutines.CompletableDeferred<Unit>()
        val coverScope = kotlinx.coroutines.CoroutineScope(SupervisorJob() + Dispatchers.IO)
        cappedUrls.forEachIndexed { idx, u ->
            coverScope.launch {
                val req = coil.request.ImageRequest.Builder(Graph.appContext)
                    .data(u)
                    // hero 大图（carousel backdrop）保持默认整屏尺寸；其余卡片按 2x
                    .apply { if (idx >= heroCount) size(464, 704) }
                    .build()
                runCatching { loader.execute(req) }
                if (pending.decrementAndGet() <= 0) allDone.complete(Unit)
            }
        }
        if (kotlinx.coroutines.withTimeoutOrNull(COVER_WAIT_MS) { allDone.await() } == null) {
            OtvLog.i("封面预载到预算上限（${COVER_WAIT_MS}ms）：剩 ${pending.get()} 张转后台继续")
        }
        return cappedUrls.size
    }
}

class App : android.app.Application(), ImageLoaderFactory {
    override fun onCreate() {
        super.onCreate()
        OtvLog.init(this)
        OtvLog.i("app onCreate — process start")
        // v1.20：内嵌后端拆到 :backend 进程（热更新免杀 app、Service 通道可可靠重启）。
        // 后端进程只起 python，其余组件（Graph/授权/热更/公告/门闸）一律不初始化
        val procName = getProcessNameCompat()
        if (procName.endsWith(":backend")) {
            OtvLog.i("backend process：启动内嵌 python 后端")
            EmbeddedBackend.ensureStarted(this)
            return
        }
        com.qiubo.optimaltv.lifecycle.AppLifecycleManager.init(this)
        // 直播流解密桥：proxy.py 安卓模式 POST :8091/decrypt，WebView 执行播放器混淆 JS（需求 足球#1）
        com.qiubo.optimaltv.playback.StreamDecryptServer.ensureStarted(this)
        // 拉起 :backend 后端进程（App.onCreate 分叉内自启 python）
        BackendService.ensure(this)
        Graph.init(this)
        // v1.17 公测版：授权管理器（内测版 LICENSE_ENABLED=false 时为空操作）
        com.qiubo.optimaltv.license.LicenseManager.init(this)
        // v1.18 公测版：热更新（内测版 HOTUPDATE_ENABLED=false 时为空操作）；
        // v1.20 起 check 与下载分离：check 快速结束不阻塞进 app，非强更弹窗让用户选
        com.qiubo.optimaltv.hotupdate.HotUpdateManager.init(this)
        // APK 应用内更新：清单检查失败时静默放行，下载完成后调起系统安装器
        com.qiubo.optimaltv.apkupdate.ApkUpdateManager.init(this)
        // v1.20 运营公告：启动拉取 + 周期轮询，顶部半透明悬浮窗展示（失败静默）
        com.qiubo.optimaltv.announcement.AnnouncementManager.init(this)
        AppStartup.begin()   // v1.17：内容预热 + 启动门闸（v1.20 重设计：≤5s 快速放行）
    }

    override fun newImageLoader(): ImageLoader = Graph.imageLoader()

    /** API 28+ 有 Application.getProcessName()；28 以下走 ActivityManager 兜底 */
    private fun getProcessNameCompat(): String =
        if (android.os.Build.VERSION.SDK_INT >= 28) getProcessName()
        else runCatching {
            val am = getSystemService(ACTIVITY_SERVICE) as android.app.ActivityManager
            am.runningAppProcesses?.firstOrNull { it.pid == android.os.Process.myPid() }?.processName
        }.getOrNull() ?: packageName
}

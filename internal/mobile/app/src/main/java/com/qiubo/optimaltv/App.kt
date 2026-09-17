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
 * 启动门闸（v1.32 起按用户要求回到「全量预热后再放行」）：
 *
 * - 旧版（v1.17~1.19）：等全部网络预热完成（TV 90s / 移动 30s）→ 进 app 动辄半分钟，
 *   是「进入 app 很卡很慢」的主根因，故 v1.20 改成只等本地秒出资源（5s 硬预算）。
 * - v1.32（2026-09-16 用户需求）：「要等所有界面的信息加载好之后再进入 APP，否则就在
 *   加载界面那里等待」——进主界面后各页数据迟迟不上屏（影视页 hero 简介 15s+、
 *   片库三栏更晚）比多等几秒启动屏更难忍，于是把足球赛程 / 电视源拉新 / 目录刷新 /
 *   影视各分类三栏 + hero 简介 + 封面预热全部纳入门闸等待，预算上限取
 *   [BuildConfig.STARTUP_GATE_MS]（TV 90s / 移动 30s）；到上限仍放行，剩余转后台继续，
 *   避免弱网/断网时把用户永久扣在启动屏。
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

    /** 全量门闸预算（用户要求等所有界面信息就绪）：TV 90s / 移动 30s，见 app/build.gradle */
    private val FULL_GATE_MS = com.qiubo.optimaltv.BuildConfig.STARTUP_GATE_MS

    fun begin() {
        val scope = kotlinx.coroutines.CoroutineScope(SupervisorJob() + Dispatchers.IO)
        val t0 = System.currentTimeMillis()
        _status.value = "正在准备…"
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
                    // 全量预热（用户要求：所有界面信息加载完再进主界面）——失败不阻塞放行
                    val matches = launch {
                        runCatching { Graph.live.matches() }
                            .onFailure { OtvLog.w("启动预热：比赛列表失败（后台重试）: ${it.message}") }
                    }
                    val iptvRefresh = launch {
                        runCatching { Graph.iptv.refreshIfStale() }
                            .onFailure { OtvLog.w("启动预热：电视源拉新失败（后台重试）: ${it.message}") }
                    }
                    val catalogRefresh = launch {
                        runCatching { Graph.repo.refresh() }
                            .onFailure { OtvLog.w("启动预热：目录刷新失败（后台重试）: ${it.message}") }
                    }
                    val vodPreload = launch {
                        runCatching { preloadVodTabPages() }
                            .onFailure { OtvLog.w("影视 tab 页预载失败（后台继续）: ${it.message}") }
                    }
                    _status.value = "正在加载影视片库…"
                    listOfNotNull(lic, iptv, cat, matches, iptvRefresh, catalogRefresh, vodPreload).joinAll()
                    _status.value = "即将进入…"
                    true
                }
                if (settled == null) {
                    OtvLog.w("startup gate 到预算上限（${FULL_GATE_MS}ms）仍未全部就绪 → 放行，剩余转后台")
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
     * 影视分类 tab 页深预载（需求③）：home 首页抓取最先触发（hero/三块「最近热门」
     * 共同依赖，与目录 refresh 并行）→ 目录快照/新数据任一就绪 → 全部分类并行拉三块
     * （最近热门/最新上线/最近更新，内部自带 awaitHome 排队）→ 收集全部卡片封面
     * 交 Coil 预热（内存+磁盘双缓存，进页即秒出）。各环节都受门闸总预算约束，
     * 超时放行后剩余预载转后台继续（Coil enqueue 不随本协程取消）。
     */
    private suspend fun preloadVodTabPages() {
        val t0 = System.currentTimeMillis()
        // 内置后端（127.0.0.1:8090）冷启动需数秒：先等就绪再开抓，否则 home/三块
        // 全部 ConnectException 秒挂、预载退化成纯快照封面（首启实测踩过）
        Graph.repo.awaitBackendReady()
        Graph.repo.refreshHome()
        Graph.repo.awaitCatalogAvailable()
        val cats = Graph.repo.state.value.catalog?.categories ?: return
        cats.forEach { Graph.repo.libBlocks(it.id) }       // 全部分类三块并行拉取（幂等）
        // 两件事并行：等各分类三块拉完（下面还要收封面 URL）+ 预取 hero 简介。
        // hero 简介（豆瓣详情）以前要等页面组合时才发请求，实测进页后 15s+ 才到货
        // （用户反馈「影视信息加载太慢」）；这里按 UI 同一套 hero 口径（heroRefs）
        // 预取前 6 条，结果落 VodRepository.briefCache，进页同步命中直接出简介。
        kotlinx.coroutines.coroutineScope {
            launch {
                runCatching {
                    Graph.repo.awaitHome()
                    heroRefs(Graph.repo.state.value.catalog, Graph.repo.homeFlow.value)
                        .map { it.id }.distinct().take(6)
                        .map { id -> launch { runCatching { Graph.repo.vodBrief(id) } } }
                        .joinAll()
                }.onFailure { OtvLog.w("hero 简介预载失败（进页后按需拉取）: ${it.message}") }
            }
            cats.map { async { Graph.repo.awaitLibBlocks(it.id) } }.awaitAll()
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
        val n = preloadCovers(urls.distinct(), heroCount = home.carousel.size)
        OtvLog.i("影视 tab 页预载完成：${catsForUrls.size} 分类 / $n 张封面（${System.currentTimeMillis() - t0}ms）")
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
        cappedUrls.forEachIndexed { idx, u ->
            val req = coil.request.ImageRequest.Builder(Graph.appContext)
                .data(u)
                // hero 大图（carousel backdrop）保持默认整屏尺寸；其余卡片按 2x
                .apply { if (idx >= heroCount) size(464, 704) }
                .build()
            loader.enqueue(req)
        }
        if (cappedUrls.size < urls.size) {
            OtvLog.i("封面预载收敛：${cappedUrls.size}/${urls.size} 张（其余进页按需加载）")
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

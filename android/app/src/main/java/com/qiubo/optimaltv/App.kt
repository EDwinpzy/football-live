package com.qiubo.optimaltv

import android.content.Context
import coil.ImageLoader
import coil.ImageLoaderFactory
import coil.disk.DiskCache
import coil.memory.MemoryCache
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.data.repo.LiveRepository
import kotlinx.coroutines.flow.asStateFlow

/** 轻量服务定位器：单进程单实例（方案 §5 单 Activity 单进程） */
object Graph {
    lateinit var appContext: Context
        private set
    lateinit var settings: SettingsStore
        private set
    lateinit var live: LiveRepository
        private set

    fun init(context: Context) {
        if (::appContext.isInitialized) return
        appContext = context.applicationContext
        settings = SettingsStore(appContext)
        val liveClient = okhttp3.OkHttpClient.Builder()
            .connectTimeout(10, java.util.concurrent.TimeUnit.SECONDS)
            .readTimeout(10, java.util.concurrent.TimeUnit.SECONDS)
            .build()
        live = LiveRepository(settings, liveClient, appContext)
    }

    /** 应用级协程作用域（v1.17：偏好写入等跨页面生命周期任务，页面退出不丢写） */
    val appScope: kotlinx.coroutines.CoroutineScope by lazy {
        kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.SupervisorJob() + kotlinx.coroutines.Dispatchers.IO)
    }

    /** 图片双缓存：比赛队标和足球背景图加载后可命中本地缓存；
     *  内存 20% + 磁盘 256MB：减少列表滚动时重复请求后端代理；
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
                // 队标和比赛图片经 127.0.0.1:8090 后端中继；提高每主机并发以减少列表图片排队。
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

/** The football schedule is the only content screen; it owns schedule loading. */
object AppStartup {
    private val _ready = kotlinx.coroutines.flow.MutableStateFlow(false)
    val ready: kotlinx.coroutines.flow.StateFlow<Boolean> = _ready.asStateFlow()
    private val _status = kotlinx.coroutines.flow.MutableStateFlow("正在加载足球赛程…")
    val status: kotlinx.coroutines.flow.StateFlow<String> = _status.asStateFlow()

    fun begin() {
        _status.value = "正在加载足球赛程…"
        _ready.value = true
    }
}
class App : android.app.Application(), ImageLoaderFactory {
    override fun onCreate() {
        super.onCreate()
        OtvLog.init(this)
        OtvLog.i("app onCreate — process start")
        // v1.20：内嵌后端拆到 :backend 进程（热更新免杀 app、Service 通道可可靠重启）。
        // 后端进程只运行 Python；足球页面、播放和更新组件只在主进程初始化。
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
        // v1.18 公测版：热更新（内测版 HOTUPDATE_ENABLED=false 时为空操作）；
        // v1.20 起 check 与下载分离：check 快速结束不阻塞进 app，非强更弹窗让用户选
        com.qiubo.optimaltv.hotupdate.HotUpdateManager.init(this)
        // APK 应用内更新：清单检查失败时静默放行，下载完成后调起系统安装器
        com.qiubo.optimaltv.apkupdate.ApkUpdateManager.init(this)
        AppStartup.begin()
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

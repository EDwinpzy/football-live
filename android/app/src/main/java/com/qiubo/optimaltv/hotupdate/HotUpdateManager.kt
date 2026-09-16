package com.qiubo.optimaltv.hotupdate

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.qiubo.optimaltv.BuildConfig
import com.qiubo.optimaltv.OtvLog
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.cancelChildren
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit
import java.util.zip.ZipFile

private val Context.hotupdateStore by preferencesDataStore(name = "hotupdate")

/** 强制更新阻断态：非 null 时 MainActivity 渲染全屏更新遮罩，AppStartup 门闸不放行 */
sealed interface ForceState {
    data class Blocking(val version: Int, val phase: String) : ForceState
}

/** 可选更新提案（v1.20 需求#13：发布更新 → app 弹窗让用户选「立即更新 / 取消」） */
data class UpdateOffer(
    val version: Int,
    val name: String,
    val sizeBytes: Long,
)

/** 用户确认更新后的执行态（弹窗进度 → 完成自动重启） */
sealed interface UserUpdateState {
    data class Downloading(
        val version: Int,
        val progress: Float,     // 0..1（未知总量时按已下 MB 估算的粗进度）
        val mbDone: Float,
        val mbTotal: Float,      // 0=未知
    ) : UserUpdateState

    data class Restarting(val version: Int) : UserUpdateState
}

/**
 * v1.18 热更新管理（公测版专用；内测版 HOTUPDATE_ENABLED=false 时 init 直接返回，零行为）。
 * v1.20（2026-09-03）按用户需求四项重做交互：
 *  ① check 与下载分离——check 本身快速结束即放行进 app，非强制更新绝不影响进入/
 *    使用（旧版静默下载 35MB 期间 checked 不置位，启动门闸被整个下载阻塞，是
 *    「进 app 很慢」根因之一）；下载只发生在强更阻断屏或用户点「立即更新」之后。
 *  ② 非强制更新 → UpdateOffer 弹窗（更新/取消）；取消记版本不再打扰，下次冷启动
 *    有新版本才再问。
 *  ③ 下载完成 → 自动生效（不再等用户手动重启，app 全程在线）：Chaquopy VM 无法
 *    进程内重启 → 后端拆 :backend 进程（BackendService），更新后杀/重建该 Service
 *    进程即完成「重启」。此前两条整机重启路径 MuMu(Android 15) 实测均失败：
 *    前台 startActivity+延迟杀进程 = launch 同包复用旧进程、杀时无待投递启动；
 *    AlarmManager PendingIntent = Android 10+ BAL 拦截不拉起。Service 启停无此限制。
 *    后端进程状态经 hu_state.json 文件回传主进程（DataStore 非多进程安全）。
 *  ④ 强制更新保持进入时阻断检查（加载页遮罩），下载带进度，完成自动生效。
 *
 * 热更对象 = 内嵌 Python 后端 zip（proxy/scraper/www，与 assets/backend/python-backend.zip
 * 同构）。下载 → SHA-256 + zip 结构双校验 → 暂存 filesDir/hotupdate/backend-hu.zip +
 * 版本标记 → 重启进程由 EmbeddedBackend 优先解压该包（坏包隔离回退 assets）。
 * 防砖阀门：强制包启动失败被隔离时记 POISON，同版本不再阻断；断网/检查失败放行。
 */
object HotUpdateManager {

    private object K {
        val VERSION = intPreferencesKey("version")      // 已暂存的热更版本号（0 = 无）
        val ACTIVE = intPreferencesKey("active")        // 后端当前实际运行的热更版本（0 = assets）
        val POISON = intPreferencesKey("poison")        // 启动失败被隔离的版本（不再阻断）
        val PROBE_FAILS = intPreferencesKey("probe_fails") // 健康探测连续失败次数（两击隔离）
        val DEVICE = stringPreferencesKey("device_id")  // 本机随机指纹（仅日志关联用）
        val DISMISSED = intPreferencesKey("dismissed")  // 用户在弹窗点过「取消」的版本（不再问）
    }

    /** 热更 zip 必备条目：与 build_backend_zip.py 的 REPL 集合一致 + www 资源目录 */
    private val REQUIRED_ENTRIES = listOf(
        "proxy.py", "hhkan.py", "scraper.py", "team_backdrop.py", "decrypt_stream.js",
        "douban_catalog.py", "douban_snapshot.json", "media_index.py", "pinyin_map.json",
        "vod_api.py", "vod_sources.py",
        "vod_sources.default.json",
    )
    private const val REQUIRED_PREFIX = "www/"

    /** 强制更新总放行预算：超时放弃阻断（下次启动再查），防弱网变砖 */
    private const val FORCE_TIMEOUT_MS = 300_000L

    /** 启动 check 预算：check 只查元数据（毫秒级~数秒），超时放行不影响进 app（需求#12） */
    private const val CHECK_TIMEOUT_MS = 12_000L

    @Volatile private var inited = false
    private lateinit var appCtx: Context
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val http = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)   // 35MB 包体（TV 网络），读超时放宽
        .build()

    private val _force = MutableStateFlow<ForceState?>(null)

    /** 强制更新阻断态：MainActivity 据此全屏遮罩 */
    val force: StateFlow<ForceState?> = _force.asStateFlow()

    /** 非强制更新提案：非 null 时 AppRoot 弹「发现新版本」对话框（需求#13） */
    private val _offer = MutableStateFlow<UpdateOffer?>(null)
    val offer: StateFlow<UpdateOffer?> = _offer.asStateFlow()

    /** 用户确认后的下载/重启执行态（弹窗进度层） */
    private val _userUpdate = MutableStateFlow<UserUpdateState?>(null)
    val userUpdate: StateFlow<UserUpdateState?> = _userUpdate.asStateFlow()

    /** 首次 check 是否已完成（启动门闸用它避免「先闪主界面再弹阻断」的竞态） */
    private val checked = MutableStateFlow(false)

    /** MainActivity 门闸读：HOTUPDATE_ENABLED 构建上，check 未 settled 前不放行主界面 */
    val settled: StateFlow<Boolean> = checked.asStateFlow()

    /** 前台标记（v1.20：更新走 :backend 进程重启，主进程在线，无需前后台区分） */
    @Volatile var appForeground: Boolean = false
        private set

    /** MainActivity onResume/onPause 上报 */
    fun onForegroundChanged(fg: Boolean) {
        appForeground = fg
        // P2 修复（2026-09-04）：后台期间 reboot 指令被后台启动限制拦下时挂起，
        // 回前台补发（BackendService.pendingReboot）
        if (fg && this::appCtx.isInitialized) {
            com.qiubo.optimaltv.BackendService.flushPendingReboot(appCtx, scope)
        }
    }

    /** 用户明确退出时终止热更检查/下载协程；切后台不会调用。 */
    fun shutdown() {
        appForeground = false
        scope.coroutineContext.cancelChildren()
    }

    /** App.onCreate 调用；快速 check（强更走阻断流，非强更弹提案），下载绝不在此路径 */
    fun init(context: Context) {
        if (!BuildConfig.HOTUPDATE_ENABLED || inited) return
        inited = true
        appCtx = context.applicationContext
        scope.launch {
            consumeBackendState()
            // 影视源配置独立于代码热更：启动即拉一次，之后后台低频轮询。
            launch {
                while (true) {
                    runCatching { syncVodSources() }
                        .onFailure { OtvLog.w("影视源配置同步失败（沿用本地版本）: ${it.message}") }
                    delay(6 * 60 * 60_000L)
                }
            }
            var pending: Triple<JSONObject, Int, String>? = null
            try {
                // P1 修复（2026-09-04）：12s 超时只包 check 元数据——旧版把强更的
                // 分钟级下载也包进 withTimeoutOrNull，慢网下载>12s 协程被取消后
                // runForced 在下载后的首个挂起点中止，_force 永卡「下载X%」阻断屏
                // 整会话不释放（且 .hotversion 未落盘，重启同版本再卡，循环复现）
                pending = withTimeoutOrNull(CHECK_TIMEOUT_MS) { checkLatest() }
            } catch (e: Exception) {
                OtvLog.w("hotupdate: 检查失败（静默跳过）: ${e.message}")
            }
            // 强更判定已出：先置阻断态再放门闸（防 awaitSettled 在 _force 置位前抢跑）
            checked.value = true
            if (pending != null) {
                runCatching { runSilent(pending.first, pending.second, pending.third) }
                    .onFailure {
                        OtvLog.w("hotupdate: 静默更新失败，下次启动重试: ${it.message}")
                    }
            }
        }
    }

    /** 启动门闸挂起等待：首次 check 完成、且强制更新流结束（或超时放行） */
    suspend fun awaitSettled() {
        if (!BuildConfig.HOTUPDATE_ENABLED) return
        withTimeoutOrNull(FORCE_TIMEOUT_MS) {
            checked.first { it }
            _force.first { it == null }
        } ?: OtvLog.w("hotupdate: 强制更新等待超时，放行进入（下次启动重查）")
    }

    /**
     * EmbeddedBackend 成功以热更包启动后回调（:backend 进程调用）。
     * v1.20 拆进程后走文件通道（DataStore 非多进程安全）：写 hu_state.json，
     * 由主进程 init()/后端重建完成时 consumeBackendState() 镜像进 DataStore 并上报。
     */
    fun notifyApplied(context: Context, version: Int) {
        if (!BuildConfig.HOTUPDATE_ENABLED) return
        HuStateFile.writeActive(context, version)
    }

    /** 健康探测失败计数（两击隔离：多 app 共用 8090 / Python 冷导入慢时避免误杀好包）。
     *  仅在 EmbeddedBackend 的后台线程调用——同步文件读改写安全（非主线程/非调度器线程）。 */
    fun countProbeFail(context: Context): Int {
        if (!BuildConfig.HOTUPDATE_ENABLED) return 0
        return HuStateFile.bumpProbeFails(context)
    }

    /** EmbeddedBackend 以 assets 启动后回调（激活版本归零——强制判断的基准） */
    fun notifyActiveAssets(context: Context) {
        if (!BuildConfig.HOTUPDATE_ENABLED) return
        HuStateFile.writeAssets(context)
    }

    /** EmbeddedBackend 启动热更包失败时回调：隔离坏包 → 拉黑该版本（防强制循环砖机）+ 上报 */
    fun notifyApplyFailed(context: Context, version: Int, detail: String) {
        if (!BuildConfig.HOTUPDATE_ENABLED) return
        HuStateFile.writeFailed(context, version, detail)
    }

    /** 主进程：消费 :backend 写入的 hu_state.json → 镜像 DataStore + 上报（applied/apply_failed） */
    private suspend fun consumeBackendState() {
        runCatching {
            val st = HuStateFile.read(appCtx) ?: return
            val active = st.optInt("active", 0)
            val poison = st.optInt("poison", 0)
            val detail = st.optString("detail", "")
            val previous = appCtx.hotupdateStore.data.first()
            val prevActive = previous[K.ACTIVE] ?: 0
            val prevPoison = previous[K.POISON] ?: 0
            appCtx.hotupdateStore.edit { p ->
                if (active > 0) p[K.ACTIVE] = active
                if (poison > 0) {
                    p[K.POISON] = poison
                    p[K.ACTIVE] = 0
                }
            }
            // PostgreSQL CU 降耗：后端每次进程启动都会重写同一个 active 状态，
            // 只有版本真正发生变化时才写 hotupdate_log，避免每次冷启动重复上报。
            if (active > 0 && active != prevActive) report(deviceId(), active, "applied", "")
            if (poison > 0 && poison != prevPoison) report(deviceId(), poison, "apply_failed", detail)
        }
    }

    // ---------------- 用户弹窗动作（需求#13） ----------------

    /** 弹窗点「立即更新」：后台下载（带进度）→ 校验暂存 → 自动重启（需求#11） */
    fun acceptUpdate() {
        val offer = _offer.value ?: return
        if (_userUpdate.value != null) return   // 已在进行
        _offer.value = null
        // P2 修复（2026-09-04）：同步先置「下载中 0%」占位——旧版要等首个 512KB 进度
        // 回调才置状态，快速双击两路都过守卫 → 并发双下载写同一 download.tmp 互相破坏
        _userUpdate.value = UserUpdateState.Downloading(offer.version, 0f, 0f, 0f)
        scope.launch {
            runCatching { downloadStageRestart(offer.version, offer.name) }
                .onFailure {
                    OtvLog.w("hotupdate: 用户更新失败: ${it.message}")
                    _userUpdate.value = null
                }
        }
    }

    /** 弹窗点「取消」：记录该版本不再打扰（下次冷启动有新版本才再问） */
    fun declineUpdate() {
        val v = _offer.value?.version ?: return
        _offer.value = null
        scope.launch { runCatching { appCtx.hotupdateStore.edit { it[K.DISMISSED] = v } } }
        OtvLog.i("hotupdate: 用户取消更新 v$v（本版本不再提示）")
    }

    // ---------------- 内部流程 ----------------

    private suspend fun currentVersion(): Int =
        appCtx.hotupdateStore.data.first()[K.VERSION] ?: 0

    private suspend fun activeVersion(): Int =
        appCtx.hotupdateStore.data.first()[K.ACTIVE] ?: 0

    private suspend fun poisonedVersion(): Int =
        appCtx.hotupdateStore.data.first()[K.POISON] ?: 0

    private suspend fun dismissedVersion(): Int =
        appCtx.hotupdateStore.data.first()[K.DISMISSED] ?: 0

    private suspend fun deviceId(): String {
        val stored = appCtx.hotupdateStore.data.first()[K.DEVICE]
        if (!stored.isNullOrBlank()) return stored
        val id = UUID.randomUUID().toString().replace("-", "")
        appCtx.hotupdateStore.edit { it[K.DEVICE] = id }
        return id
    }

    /** 仅 check 元数据（不做任何下载）：强更 → 阻断流；非强更新版本 → 弹提案 */
    private suspend fun checkLatest(): Triple<JSONObject, Int, String>? {
        val endpoint = BuildConfig.HOTUPDATE_ENDPOINT
        if (endpoint.isBlank()) return null
        val local = currentVersion()
        val dev = deviceId()
        val resp = postJson("$endpoint/check", JSONObject()
            .put("hotVersion", local)
            .put("apkVersion", BuildConfig.VERSION_CODE)
            .put("deviceId", dev)) ?: return null
        if (resp.optInt("ret", -1) != 0 || !resp.optBoolean("hasUpdate")) return null
        val pkg = resp.optJSONObject("pkg") ?: return null
        val code = pkg.optInt("hotVersionCode", 0)
        val url = pkg.optString("url")
        val expectSha = pkg.optString("sha256").lowercase()
        val expectSize = pkg.optLong("fileSize", 0L)
        if (code <= 0 || url.isBlank() || expectSha.length != 64) return null

        val active = activeVersion()
        val poisoned = poisonedVersion()
        if (code > maxOf(active, local) && code != poisoned) {
            return Triple(pkg, code, dev)
        }
        if (code <= local) return null                 // 已暂存过（等下次冷启动生效/已生效）
        return Triple(pkg, code, dev)
    }

    private suspend fun runSilent(pkg: JSONObject, code: Int, dev: String) {
        val ok = downloadStage(code, pkg.optString("url"), pkg.optString("sha256").lowercase(),
            pkg.optLong("fileSize", 0L))
        if (!ok) {
            report(dev, code, "failed", "silent_download")
            return
        }
        OtvLog.i("hotupdate: v$code 已静默校验完成，重启后端资源进程")
        restartApp()
    }

    /** 下载 → 校验 → 暂存（用户确认/强更共用）。失败返回 false（phase 由调用方定）。 */
    private suspend fun downloadStage(
        code: Int, url: String, expectSha: String, expectSize: Long,
        onProgress: (doneBytes: Long, totalBytes: Long) -> Unit = { _, _ -> },
    ): Boolean {
        val dir = File(appCtx.filesDir, "hotupdate").apply { mkdirs() }
        val tmp = File(dir, "download.tmp")
        val sha = withTimeoutOrNull(FORCE_TIMEOUT_MS) { downloadTo(url, tmp, onProgress) }
        if (sha == null || sha != expectSha || (expectSize > 0 && tmp.length() != expectSize) ||
            !zipLooksValid(tmp)) {
            tmp.delete()
            report(deviceId(), code, "failed", "download")
            return false
        }
        val dst = File(dir, "backend-hu.zip")
        dst.delete()
        if (!tmp.renameTo(dst)) { tmp.delete(); report(deviceId(), code, "failed", "stage"); return false }
        File(dir, ".hotversion").writeText(code.toString())
        appCtx.hotupdateStore.edit { it[K.VERSION] = code }
        report(deviceId(), code, "downloaded", "")
        return true
    }

    /** 用户确认后的完整链路：进度上报 → 下载暂存 → 自动重启（需求#11/#13） */
    private suspend fun downloadStageRestart(code: Int, name: String) {
        val endpoint = BuildConfig.HOTUPDATE_ENDPOINT
        if (endpoint.isBlank()) return
        // 重新 check 拿最新包元数据（提案里不存 url/sha，避免过期签名链接）
        val resp = postJson("$endpoint/check", JSONObject()
            .put("hotVersion", currentVersion())
            .put("apkVersion", BuildConfig.VERSION_CODE)
            .put("deviceId", deviceId())) ?: throw IllegalStateException("check 失败，请稍后重试")
        val pkg = resp.optJSONObject("pkg") ?: throw IllegalStateException("更新包信息获取失败")
        val url = pkg.optString("url")
        val expectSha = pkg.optString("sha256").lowercase()
        val expectSize = pkg.optLong("fileSize", 0L)
        if (url.isBlank() || expectSha.length != 64) throw IllegalStateException("更新包信息不完整")

        OtvLog.i("hotupdate: 用户确认更新 v$code（$name），开始下载")
        val ok = downloadStage(code, url, expectSha, expectSize) { done, total ->
            _userUpdate.value = UserUpdateState.Downloading(
                version = code,
                progress = if (total > 0) done.toFloat() / total else (done / 1048576f / 40f).coerceAtMost(0.95f),
                mbDone = done / 1048576f,
                mbTotal = if (total > 0) total / 1048576f else 0f,
            )
        }
        if (!ok) {
            _userUpdate.value = null
            OtvLog.w("hotupdate: v$code 下载/校验失败，本次放弃（下次冷启动再提议）")
            // 失败不记 dismissed：下次启动重新提议
            return
        }
        _userUpdate.value = UserUpdateState.Restarting(code)
        delay(1500)
        restartApp()
    }

    /** 强制热更：阻断态下载（带进度）→ 暂存 → 自动重启，重启后 EmbeddedBackend 生效并上报 applied */
    private suspend fun runForced(pkg: JSONObject, code: Int, dev: String) {
        val url = pkg.optString("url")
        val expectSha = pkg.optString("sha256").lowercase()
        val expectSize = pkg.optLong("fileSize", 0L)
        val mb = if (expectSize > 0) "${expectSize / 1048576} MB" else "? MB"
        OtvLog.i("hotupdate: 强制更新 v$code，阻断进入")
        _force.value = ForceState.Blocking(code, "下载更新包（$mb）…")

        val ok = downloadStage(code, url, expectSha, expectSize) { done, total ->
            val p = if (total > 0) (done * 100 / total) else -1
            _force.value = ForceState.Blocking(
                code,
                if (p >= 0) "下载更新包 $p%（${done / 1048576}/${total / 1048576} MB）…"
                else "下载更新包 ${done / 1048576} MB…",
            )
        }
        if (!ok) {
            report(dev, code, "failed", "force_download")
            OtvLog.w("hotupdate: 强制包 v$code 下载/校验失败，本次放行（下次启动重试）")
            _force.value = ForceState.Blocking(code, "下载失败，将不影响本次使用（下次启动重试）")
            delay(2500)
            _force.value = null
            return
        }
        _force.value = ForceState.Blocking(code, "更新完成，正在生效…")
        delay(1500)
        restartApp()
    }

    /**
     * 热更新生效（v1.20 终版）。Chaquopy VM 无法进程内重启 → 后端拆在 :backend 进程，
     * 这里只杀/重建该进程（Service 通道不受后台 Activity 启动限制），app 主进程在线，
     * 用户无感知完成「重启生效」——替代此前两条实测均失败的整机重启路径：
     * ① startActivity+延迟杀进程：launch 在旧进程内完成后连带死，无待投递启动；
     * ② AlarmManager PendingIntent：Android 10+ BAL 拦截（MuMu 实测不拉起）。
     */
    private fun restartApp() {
        com.qiubo.optimaltv.BackendService.reboot(appCtx, scope)
    }

    /** :backend 重建完成回调（BackendService 调）：清进度层、消费新后端状态并上报 */
    fun onBackendRebooted() {
        _userUpdate.value = null
        scope.launch {
            delay(800)   // 等 :backend 起完 python 并写 hu_state.json
            consumeBackendState()
        }
    }

    /** 下载并流式计算 SHA-256；onProgress 回调实况；失败返回 null */
    private suspend fun downloadTo(
        url: String,
        dst: File,
        onProgress: (Long, Long) -> Unit = { _, _ -> },
    ): String? = withContext(Dispatchers.IO) {
        runCatching {
            // 自有站点接口统一浏览器 UA：见 data/net/HttpUa（非浏览器 UA 会被边缘拦成 403）
            val req = Request.Builder().url(url)
                .header("User-Agent", com.qiubo.optimaltv.data.net.HttpUa.BROWSER)
                .build()
            http.newCall(req).execute().use { r ->
                if (!r.isSuccessful) return@runCatching null
                val total = r.body?.contentLength() ?: -1L
                val digest = MessageDigest.getInstance("SHA-256")
                var done = 0L
                var lastCb = 0L
                dst.outputStream().use { out ->
                    r.body?.byteStream()?.use { input ->
                        val buf = ByteArray(64 * 1024)
                        while (true) {
                            val n = input.read(buf)
                            if (n < 0) break
                            out.write(buf, 0, n)
                            digest.update(buf, 0, n)
                            done += n
                            // 进度节流：≥512KB 或完成才回调（避免刷爆重组）
                            if (done - lastCb >= 512 * 1024 || (total > 0 && done >= total)) {
                                lastCb = done
                                onProgress(done, total)
                            }
                        }
                    } ?: return@runCatching null
                }
                onProgress(done, total)
                digest.digest().joinToString("") { "%02x".format(it) }
            }
        }.getOrNull()
    }

    /** zip 结构校验：五个后端模块 + www 资源目录缺一不可（防误传任意 zip） */
    private fun zipLooksValid(f: File): Boolean = runCatching {
        ZipFile(f).use { zf ->
            val names = zf.entries().asSequence().map { it.name }.toList()
            REQUIRED_ENTRIES.all { names.contains(it) } && names.any { it.startsWith(REQUIRED_PREFIX) }
        }
    }.getOrDefault(false)

    private suspend fun postJson(url: String, body: JSONObject): JSONObject? = withContext(Dispatchers.IO) {
        runCatching {
            val req = Request.Builder().url(url)
                .header("User-Agent", com.qiubo.optimaltv.data.net.HttpUa.BROWSER)
                .header("Accept", com.qiubo.optimaltv.data.net.HttpUa.ACCEPT)
                .post(body.toString().toRequestBody("application/json".toMediaType()))
                .build()
            http.newCall(req).execute().use { r ->
                if (!r.isSuccessful) return@runCatching null
                runCatching { JSONObject(r.body?.string().orEmpty()) }.getOrNull()
            }
        }.getOrNull()
    }

    private suspend fun report(dev: String, code: Int, result: String, detail: String) {
        withTimeoutOrNull(15_000) {
            postJson("${BuildConfig.HOTUPDATE_ENDPOINT}/report", JSONObject()
                .put("deviceId", dev)
                .put("apkVersion", BuildConfig.VERSION_CODE)
                .put("fromVersion", currentVersion())
                .put("toVersion", code)
                .put("result", result)
                .put("detail", detail))
        }
        OtvLog.i("hotupdate: report $result v$code${if (detail.isNotBlank()) " ($detail)" else ""}")
    }

    /** 拉取已发布影视源；草稿不会下发。临时文件落盘后原子替换，内置后端会热加载新版本。 */
    private suspend fun syncVodSources() {
        val target = File(com.qiubo.optimaltv.EmbeddedBackend.backendDir(appCtx), "vod_sources.runtime.json")
        val localVersion = runCatching {
            if (target.isFile) JSONObject(target.readText()).optInt("version", 0) else 0
        }.getOrDefault(0)
        val response = withTimeoutOrNull(CHECK_TIMEOUT_MS) {
            postJson(BuildConfig.HOTUPDATE_ENDPOINT.trimEnd('/') + "/vod-sources/check",
                JSONObject().put("deviceId", deviceId()).put("version", localVersion))
        } ?: return
        if (!response.optBoolean("hasUpdate", false)) return
        val config = response.optJSONObject("config") ?: return
        require(config.optJSONArray("sources") != null) { "影视源配置格式错误" }
        target.parentFile?.mkdirs()
        val tmp = File(target.parentFile, target.name + ".tmp")
        tmp.writeText(config.toString())
        if (!tmp.renameTo(target)) {
            target.delete()
            require(tmp.renameTo(target)) { "影视源配置替换失败" }
        }
        OtvLog.i("影视源配置已更新至 v${config.optInt("version", response.optInt("version"))}")
    }
}

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

/**
 * v1.18 热更新管理（公测版专用；内测版 HOTUPDATE_ENABLED=false 时 init 直接返回，零行为）。
 * v1.19 移植到移动版（版本重规划后移动版同为正式分发线，与 TV 版共用同一热更通道）。
 * v1.20（2026-09-03）按用户需求四项重做交互：
 *  ① check 与下载分离——check 本身快速结束即放行进 app，非强制更新绝不影响进入/
 *    使用（旧版静默下载 35MB 期间 checked 不置位，启动门闸被整个下载阻塞，是
 *    「进 app 很慢」根因之一）；下载只发生在强更阻断屏或用户点「立即更新」之后。
 *  ② 非强制更新 → 后台静默下载，**不弹窗、不打扰用户**（2026-10-01 用户明确要求：
 *    「后端热更新，自动在后台更新，不需要弹窗让用户确认」）。已移除 UpdateOffer 弹窗、
 *    进度浮层与 dismissed 去重键——check 完即后台下载，下载不阻塞 check/门闸。
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
        .readTimeout(60, TimeUnit.SECONDS)   // 35MB 包体，读超时放宽
        .build()

    private val _force = MutableStateFlow<ForceState?>(null)

    /** 强制更新阻断态：MainActivity 据此全屏遮罩 */
    val force: StateFlow<ForceState?> = _force.asStateFlow()

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
            // 2026-10-01：强更也不再阻断进入。静默自动更新对强更/非强更一视同仁，
            // 阻断屏（ForceState）随之停用——之前只在 check 后置位 Blocking、却把执行体
            // 换成了 runSilent()，导致 _force 永不被清空，awaitSettled 的
            // `_force.first { it == null }` 一直等不到，整个会话卡在「准备下载更新包…」转圈。
            checked.value = true
            // 后台静默更新（2026-10-01 用户明确要求：不需要弹窗让用户确认）：
            // check 完即后台下载，下载不阻塞 check/门闸，也不占 UI。
            if (pending != null) {
                runCatching { runSilent(pending.first, pending.second, pending.third) }
                    .onFailure {
                        OtvLog.w("hotupdate: 静默更新失败，下次启动重试: ${it.message}")
                    }
            }
        }
    }

    /**
     * 启动门闸挂起等待：首次 check 完成即放行。
     * 2026-10-01 起不再等 `_force == null`——强更已改为静默后台更新，阻断屏停用，
     * 等一个永不会被清空的 _force 会让门闸挂到 FORCE_TIMEOUT_MS（300s）才超时放行，
     * 表现为「强制更新页一直转圈进不去」。
     */
    suspend fun awaitSettled() {
        if (!BuildConfig.HOTUPDATE_ENABLED) return
        withTimeoutOrNull(CHECK_TIMEOUT_MS + 3_000L) {
            checked.first { it }
        } ?: OtvLog.w("hotupdate: 首次 check 未在门闸时限内完成，放行进入（下次启动重查）")
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
            // PostgreSQL CU 降耗：只在热更版本真正变化时写 applied 日志。
            if (active > 0 && active != prevActive) report(deviceId(), active, "applied", "")
            if (poison > 0 && poison != prevPoison) report(deviceId(), poison, "apply_failed", detail)
        }
    }

    // ---------------- 内部流程 ----------------

    private suspend fun currentVersion(): Int =
        appCtx.hotupdateStore.data.first()[K.VERSION] ?: 0

    private suspend fun activeVersion(): Int =
        appCtx.hotupdateStore.data.first()[K.ACTIVE] ?: 0

    private suspend fun poisonedVersion(): Int =
        appCtx.hotupdateStore.data.first()[K.POISON] ?: 0

    private suspend fun deviceId(): String {
        val stored = appCtx.hotupdateStore.data.first()[K.DEVICE]
        if (!stored.isNullOrBlank()) return stored
        val id = UUID.randomUUID().toString().replace("-", "")
        appCtx.hotupdateStore.edit { it[K.DEVICE] = id }
        return id
    }

    /** 仅 check 元数据（不做任何下载）：强更 → 返回 (pkg, code, deviceId) 交由调用方在
     *  check 超时作用域【外】执行阻断下载（P1 修复 2026-09-04）；非强更新版本 → 弹提案，返回 null */
    /**
     *  一步到位（2026-10-01 用户明确要求）：服务端 hotupdate_check 恒取最高已发布版本
     *  （order by hot_version_code desc limit 1），所以本地 v15 收到的是 v18 的包，
     *  一次下载即跨过中间版本——不存在 v15→v16→v17→v18 逐级跳。
     *
     *  静默自动更新（2026-10-01 用户明确要求「不需要弹窗让用户确认」）：
     *  强更与非强更一视同仁返回包，交由 init 在后台下载，不再有提案弹窗。
     */
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
        if (code <= maxOf(active, local)) return null   // 已暂存/已生效（等下次冷启动生效）
        if (code == poisoned) return null               // 坏包已拉黑
        // Reinstall wipes DataStore + filesDir, so local==0 and the client asks the server
        // for an update; the server then returns the very zip already embedded in this APK
        // (identical sha256), so the user re-downloads 40MB they already have, every time.
        // When we have no local version recorded, hash the embedded zip once: a match means
        // "already up to date". A genuinely newer server package (different sha) still downloads.
        if (local == 0 && embeddedZipSha() == expectSha) {
            OtvLog.i("hotupdate: v$code equals embedded backend zip, already up to date, skip download")
            return null
        }
        return Triple(pkg, code, dev)
    }

    /** SHA-256 (lowercase hex) of the APK-embedded backend zip; null if unreadable. Only called when local==0. */
    private fun embeddedZipSha(): String? = runCatching {
        val digest = MessageDigest.getInstance("SHA-256")
        appCtx.assets.open("backend/python-backend.zip").use { input ->
            val buf = ByteArray(1 shl 16)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                digest.update(buf, 0, n)
            }
        }
        digest.digest().joinToString("") { "%02x".format(it) }
    }.getOrNull()

    /** 该包是否强制（阻断进入）。非强更为静默后台更新。 */
    private fun isForced(pkg: JSONObject): Boolean = pkg.optBoolean("forceApply")

    /** 静默更新：check 完即后台下载（不弹窗、不占 UI），下载完自动生效 */
    private suspend fun runSilent(pkg: JSONObject, code: Int, dev: String) {
        val name = pkg.optString("hotVersionName")
        val size = pkg.optLong("fileSize", 0L)
        OtvLog.i("hotupdate: v$code（$name，" +
            "${if (size > 0) "${size / 1048576}MB" else "?"}）后台静默下载")
        val ok = downloadStage(code, pkg.optString("url"), pkg.optString("sha256").lowercase(), size)
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

    /** :backend 重建完成回调（BackendService 调）：消费新后端状态并上报 */
    fun onBackendRebooted() {
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

package com.qiubo.optimaltv.apkupdate

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import com.qiubo.optimaltv.BuildConfig
import com.qiubo.optimaltv.OtvLog
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.Locale
import java.util.concurrent.TimeUnit

sealed interface ApkUpdateState {
    data class Downloading(
        val progress: Float,
        val downloadedBytes: Long,
        val totalBytes: Long,
        val attempt: Int = 1,
        /** 瞬时下载速度（字节/秒）；0 表示尚未测得，用于文案展示而非进度计算。 */
        val bytesPerSecond: Long = 0L,
    ) : ApkUpdateState

    data object PermissionRequired : ApkUpdateState
    data object Installing : ApkUpdateState
    data class Error(val message: String) : ApkUpdateState
}

/** 手动「检查更新」（我的页按钮）的结果，供页面行内展示。 */
sealed interface ApkCheckState {
    data object Checking : ApkCheckState

    /** 已是最新：local 为当前安装版本。 */
    data class UpToDate(val localVersionName: String, val localVersionCode: Long) : ApkCheckState

    /** 发现新版本：更新弹窗由 [ApkUpdateManager.offer] 驱动。 */
    data class UpdateFound(val versionName: String, val versionCode: Long) : ApkCheckState

    data class Failed(val message: String) : ApkCheckState
}

data class ApkUpdateOffer(
    val manifest: ApkUpdateManifest,
    val decision: ApkUpdateDecision,
)

/**
 * APK 应用内更新：检查清单、下载校验 APK，并调起 Android 系统安装器。
 *
 * 这里不尝试静默安装。普通应用只能自动打开系统安装确认界面，最终安装仍由用户确认。
 */
object ApkUpdateManager {
    private const val PREFS = "apk_update"
    private const val DISMISSED_VERSION = "dismissed_version"
    private const val MAX_ATTEMPTS = 3

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    /**
     * 超时取值针对真机实测的弱网链路（2026-10-01）。
     *
     * 原来 `readTimeout(90s)` 意味着：弱网下一个字节都收不到时，界面会静默
     * 停留 90 秒且进度一直是 0%，实测真机就卡在「正在下载 0%」。
     * 改成 20 秒后同样的卡顿最多 20 秒就进入重试，重试计数也会显示在文案里，
     * 用户能看见「在重试第 N 次」而不是以为应用挂了。
     * 同时打开 callTimeout 兜住「连接成功但迟迟不吐数据」这种最坏情况。
     */
    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .callTimeout(15 * 60, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private lateinit var appContext: Context
    private var initialized = false
    private val downloadLock = Any()
    private var activeDownload: DownloadRun? = null

    private class DownloadRun {
        @Volatile
        var cancelled = false
        var activeCall: Call? = null
        var job: Job? = null
        var retryQueued = false
        val finished = CompletableDeferred<Unit>()
    }

    private val _offer = MutableStateFlow<ApkUpdateOffer?>(null)
    val offer: StateFlow<ApkUpdateOffer?> = _offer.asStateFlow()

    /** 已下载并校验通过的 APK；安装被系统拦下后重试时免重复下载。 */
    @Volatile
    private var verifiedApk: File? = null

    private val _state = MutableStateFlow<ApkUpdateState?>(null)
    val state: StateFlow<ApkUpdateState?> = _state.asStateFlow()

    private val _checkState = MutableStateFlow<ApkCheckState?>(null)
    val checkState: StateFlow<ApkCheckState?> = _checkState.asStateFlow()

    fun init(context: Context) {
        if (initialized) return
        initialized = true
        appContext = context.applicationContext
        if (!BuildConfig.APK_UPDATE_ENABLED) return
        scope.launch {
            runCatching { checkLatest(manual = false) }
                .onFailure { OtvLog.w("apk update: 检查失败（静默跳过）: ${it.message}") }
        }
    }

    /**
     * 我的页「检查更新」：用户主动触发，结果写进 [checkState]；
     * 与启动时的静默检查不同，这里忽略「稍后更新」记忆并回报失败原因。
     */
    fun checkForUpdatesManually() {
        if (!initialized || _checkState.value is ApkCheckState.Checking) return
        _checkState.value = ApkCheckState.Checking
        scope.launch {
            runCatching { checkLatest(manual = true) }
                .onFailure {
                    OtvLog.w("apk update: 手动检查失败: ${it.message}")
                    _checkState.value = ApkCheckState.Failed(it.message ?: "检查失败，请稍后重试")
                }
        }
    }

    /** 从未知来源设置返回后自动继续；用户无需再次按一次更新。 */
    fun onForegroundChanged(isForeground: Boolean) {
        if (!isForeground || !initialized) return
        if (_state.value is ApkUpdateState.Installing) {
            _state.value = null
            return
        }
        if (_state.value is ApkUpdateState.PermissionRequired && canInstallUnknownApps()) {
            acceptUpdate()
        }
    }

    fun declineUpdate() {
        val current = _offer.value ?: return
        appContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putLong(DISMISSED_VERSION, current.manifest.versionCode)
            .apply()
        _offer.value = null
        _state.value = null
        verifiedApk = null
    }

    fun acceptUpdate() {
        val current = _offer.value ?: return
        val run = synchronized(downloadLock) {
            val previous = activeDownload
            if (previous != null) {
                if (previous.cancelled && !previous.retryQueued) {
                    // Let the cancelled run close its stream and remove its files before
                    // retrying into the same paths. This also makes an immediate retry work.
                    previous.retryQueued = true
                    scope.launch {
                        previous.finished.await()
                        if (_offer.value == current && _state.value == null) acceptUpdate()
                    }
                }
                return
            }
            DownloadRun().also {
                activeDownload = it
                _state.value = ApkUpdateState.Downloading(0f, 0L, current.manifest.apk.sizeBytes)
            }
        }
        val job = scope.launch(start = CoroutineStart.LAZY) { runUpdate(current, run) }
        synchronized(downloadLock) {
            if (activeDownload === run) run.job = job
        }
        job.start()
    }

    /** Stop the active APK download and return to the version offer without reporting an error. */
    fun cancelUpdate() {
        val (call, job) = synchronized(downloadLock) {
            val run = activeDownload ?: return
            if (run.cancelled || _state.value !is ApkUpdateState.Downloading) return
            run.cancelled = true
            _state.value = null
            run.activeCall to run.job
        }
        call?.cancel()
        job?.cancel(CancellationException("APK update cancelled"))
    }

    fun retryUpdate() {
        acceptUpdate()
    }

    private suspend fun runUpdate(offer: ApkUpdateOffer, run: DownloadRun) {
        try {
            // Some TV ROMs have no usable "install unknown apps" settings page. Download
            // first, then give Android's installer the APK as before.
            ensureNotCancelled(run)
            val expected = offer.manifest.apk.sizeBytes
            val apk = verifiedApk?.takeIf { it.exists() && (expected <= 0 || it.length() == expected) }
                ?: downloadAndVerify(offer.manifest, run).also { verifiedApk = it }
            ensureNotCancelled(run)
            val canHandOff = synchronized(downloadLock) {
                if (activeDownload !== run || run.cancelled) {
                    false
                } else {
                    _state.value = ApkUpdateState.Installing
                    true
                }
            }
            if (!canHandOff) throw CancellationException("APK update cancelled")
            withContext(Dispatchers.Main) { launchInstaller(apk) }
        } catch (t: Throwable) {
            if (run.cancelled || t is CancellationException) {
                cleanupCancelledDownload(offer.manifest)
                synchronized(downloadLock) {
                    if (activeDownload === run) _state.value = null
                }
            } else {
                OtvLog.w("apk update: 下载或校验失败: ${t.message}")
                synchronized(downloadLock) {
                    if (activeDownload === run && !run.cancelled) {
                        _state.value = ApkUpdateState.Error(t.message ?: "更新失败，请稍后重试")
                        activeDownload = null
                    }
                }
            }
        } finally {
            synchronized(downloadLock) {
                if (activeDownload === run) activeDownload = null
            }
            run.finished.complete(Unit)
        }
    }

    private fun cleanupCancelledDownload(manifest: ApkUpdateManifest) {
        val dir = File(appContext.filesDir, "update")
        val safeName = manifest.apk.fileName.substringAfterLast('/').ifBlank { "update.apk" }
        File(dir, "$safeName.part").delete()
        File(dir, safeName).delete()
        verifiedApk = null
    }

    private fun ensureNotCancelled(run: DownloadRun) {
        if (run.cancelled) throw CancellationException("APK update cancelled")
    }

    private fun publishProgress(run: DownloadRun, state: ApkUpdateState.Downloading) {
        synchronized(downloadLock) {
            if (activeDownload !== run || run.cancelled) {
                throw CancellationException("APK update cancelled")
            }
            _state.value = state
        }
    }

    fun openUnknownAppSettings() {
        if (!initialized) return

        // 部分投影仪/TV 定制 ROM 裁掉了「安装未知应用」设置页，直接跳转会抛
        // ActivityNotFoundException 把应用带崩；这里逐级降级到更通用的设置页，
        // 全部失败时留在弹窗里提示手动处理，而不是闪退。
        val opened: Boolean
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            opened = tryStartActivity(
                Intent(
                    Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:${appContext.packageName}"),
                ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            ) || tryStartActivity(
                Intent(Settings.ACTION_SECURITY_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            ) || tryStartActivity(
                Intent(
                    Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:${appContext.packageName}"),
                ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
        } else {
            opened = tryStartActivity(
                Intent(Settings.ACTION_SECURITY_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
        }
        if (!opened) {
            _state.value = ApkUpdateState.Error(
                "未能打开系统设置，请在系统设置中手动允许 My TV 安装未知应用后重试",
            )
        }
    }

    private fun tryStartActivity(intent: Intent): Boolean = runCatching {
        appContext.startActivity(intent)
        true
    }.onFailure {
        OtvLog.w("apk update: 无法打开设置页: ${it.message}")
    }.getOrDefault(false)

    private suspend fun checkLatest(manual: Boolean) {
        if (!BuildConfig.APK_UPDATE_ENABLED) {
            if (manual) _checkState.value = ApkCheckState.Failed("当前安装包未启用应用内更新")
            return
        }
        val endpoint = BuildConfig.APK_UPDATE_URL.trim()
        if (endpoint.isBlank()) {
            if (manual) _checkState.value = ApkCheckState.Failed("未配置应用内更新地址")
            return
        }
        val uri = Uri.parse(endpoint)
        if (uri.scheme != "https") throw IllegalArgumentException("APK 更新地址必须使用 HTTPS")

        val request = Request.Builder()
            .url(endpoint)
            .header("Accept", "application/json")
            .header("Cache-Control", "no-cache")
            .build()
        http.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw IllegalStateException("HTTP ${response.code}")
            val body = response.body?.string().orEmpty()
            val manifest = parseManifest(JSONObject(body))
            if (manifest.packageName != appContext.packageName) {
                if (manual) _checkState.value = ApkCheckState.Failed("更新清单与当前安装包渠道不匹配")
                return
            }
            val localVersion = appContext.packageManager
                .getPackageInfo(appContext.packageName, 0).longVersionCode
            val decision = ApkUpdatePolicy.decide(localVersion, manifest)
            if (!ApkUpdatePolicy.shouldPrompt(decision, isDismissed(manifest.versionCode), manual)) {
                if (manual && decision == ApkUpdateDecision.NONE) {
                    _checkState.value = ApkCheckState.UpToDate(
                        localVersionName = installedVersionName(),
                        localVersionCode = localVersion,
                    )
                }
                return
            }
            verifiedApk = null
            _offer.value = ApkUpdateOffer(manifest, decision)
            if (manual) {
                _checkState.value = ApkCheckState.UpdateFound(
                    versionName = manifest.versionName,
                    versionCode = manifest.versionCode,
                )
            }
        }
    }

    /** 当前安装包的 versionName（展示用；读取失败时退化为版本号字符串）。 */
    private fun installedVersionName(): String = runCatching {
        appContext.packageManager.getPackageInfo(appContext.packageName, 0).versionName
    }.getOrNull().orEmpty().ifBlank { BuildConfig.VERSION_NAME }

    private fun parseManifest(root: JSONObject): ApkUpdateManifest {
        val apk = root.optJSONObject("apk") ?: root
        val url = apk.optString("url").trim()
        val sha256 = apk.optString("sha256").trim().lowercase(Locale.US)
        val sizeBytes = apk.optLong("size", 0)
        require(sha256.matches(Regex("[0-9a-f]{64}"))) { "APK SHA-256 无效" }
        val parts = parseParts(apk.optJSONArray("parts"))
        if (parts.isEmpty()) {
            require(url.isNotBlank() && Uri.parse(url).scheme == "https") { "APK 地址必须使用 HTTPS" }
        } else {
            // 分片清单 apk.url 可为空；地址与长度约束由 ApkPartPolicy 与 parseParts 兜住。
            require(url.isBlank() || Uri.parse(url).scheme == "https") { "APK 地址必须使用 HTTPS" }
            ApkPartPolicy.problem(parts, sizeBytes)?.let { error(it) }
        }
        return ApkUpdateManifest(
            packageName = root.optString("packageName"),
            channel = root.optString("channel", "stable"),
            deviceType = root.optString("deviceType", "tv"),
            versionCode = root.optLong("versionCode", 0),
            versionName = root.optString("versionName", ""),
            minSupportedVersionCode = root.optLong("minSupportedVersionCode", 0),
            forceUpdate = root.optBoolean("forceUpdate", false),
            title = root.optString("title", "发现新版本"),
            changelog = root.optJSONArray("changelog").toStringList(),
            apk = ApkArtifact(
                fileName = apk.optString("fileName", "update.apk"),
                url = url,
                sizeBytes = sizeBytes,
                sha256 = sha256,
                certificateSha256 = apk.optJSONArray("certificateSha256").toStringList()
                    .map { it.uppercase(Locale.US) },
                parts = parts,
            ),
        ).also {
            require(it.versionCode > 0) { "APK versionCode 无效" }
            require(it.packageName.isNotBlank()) { "packageName 为空" }
        }
    }

    private suspend fun downloadAndVerify(manifest: ApkUpdateManifest, run: DownloadRun): File {
        ensureNotCancelled(run)
        val dir = File(appContext.filesDir, "update").apply { mkdirs() }
        val safeName = manifest.apk.fileName.substringAfterLast('/').ifBlank { "update.apk" }
        val part = File(dir, "$safeName.part")
        val target = File(dir, safeName)
        ensureFreeSpace(dir, manifest)
        if (part.exists()) part.delete()
        if (target.exists()) target.delete()
        var lastError: Throwable? = null
        for (attempt in 0 until MAX_ATTEMPTS) {
            ensureNotCancelled(run)
            try {
                downloadOnce(manifest.apk, part, attempt + 1, run)
                ensureNotCancelled(run)
                verifySha256(part, manifest, run)
                ensureNotCancelled(run)
                if (target.exists()) target.delete()
                check(part.renameTo(target)) { "无法保存更新文件" }
                validatePackage(target, manifest)
                ensureNotCancelled(run)
                return target
            } catch (t: Throwable) {
                if (run.cancelled || t is CancellationException) {
                    part.delete()
                    target.delete()
                    throw CancellationException("APK update cancelled")
                }
                lastError = t
                part.delete()
                if (t is PackageValidationError) {
                    target.delete()
                    throw t
                }
                if (attempt + 1 < MAX_ATTEMPTS) {
                    ensureNotCancelled(run)
                    delay((attempt + 1) * 1500L)
                }
            }
        }
        throw lastError ?: IllegalStateException("下载失败")
    }

    /** 下载前预检剩余空间：不足时立刻给出可读原因，而不是边下边失败循环烧流量。 */
    private fun ensureFreeSpace(dir: File, manifest: ApkUpdateManifest) {
        val needed = manifest.apk.sizeBytes
        if (needed <= 0) return
        val free = dir.usableSpace
        check(free >= needed + 32L * 1024 * 1024) {
            "存储空间不足：更新包约需 ${needed / 1048576} MB，当前可用仅 ${free / 1048576} MB，请清理设备空间后重试"
        }
    }

    /** Keep the call cancellable through response-body consumption, not only until headers arrive. */
    private fun <T> withDownloadCall(
        run: DownloadRun,
        request: Request,
        block: (Response) -> T,
    ): T {
        val call = http.newCall(request)
        synchronized(downloadLock) {
            if (activeDownload !== run || run.cancelled) {
                throw CancellationException("APK update cancelled")
            }
            run.activeCall = call
        }
        return try {
            ensureNotCancelled(run)
            call.execute().use(block)
        } finally {
            synchronized(downloadLock) {
                if (run.activeCall === call) run.activeCall = null
            }
        }
    }

    private fun downloadOnce(artifact: ApkArtifact, part: File, attempt: Int, run: DownloadRun) {
        // 分片清单按顺序下载并连续写入同一个 .part 文件；单对象清单退化成一段（不校验段摘要）。
        val sources = artifact.parts.ifEmpty { listOf(ApkPart(artifact.url, artifact.sizeBytes, "")) }
        val total = if (artifact.sizeBytes > 0) artifact.sizeBytes else sources.sumOf { it.sizeBytes }
        var done = 0L
        // 速度采样起点：弱网下速度是判断「还在动 / 已经卡死」的唯一依据。
        val speedSampleStart = System.nanoTime()
        var speedSampleBytes = 0L
        var bytesPerSecond = 0L
        part.outputStream().use { output ->
            for ((index, source) in sources.withIndex()) {
                ensureNotCancelled(run)
                val request = Request.Builder().url(source.url).build()
                withDownloadCall(run, request) { response ->
                    check(response.isSuccessful) { "第 ${index + 1}/${sources.size} 段 HTTP ${response.code}" }
                    val body = requireNotNull(response.body) { "下载响应为空" }
                    val digest = source.sha256.takeIf { it.isNotBlank() }
                        ?.let { MessageDigest.getInstance("SHA-256") }
                    var segment = 0L
                    var lastEmit = 0L
                    body.byteStream().use { input ->
                        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                        while (true) {
                            ensureNotCancelled(run)
                            val count = input.read(buffer)
                            if (count < 0) break
                            output.write(buffer, 0, count)
                            digest?.update(buffer, 0, count)
                            segment += count
                            done += count
                            speedSampleBytes += count

                            // 每收到 256 KiB 或每 500 ms 推一次进度：
                            // 弱网下逐字节推送只会让 StateFlow 疯狂触发，
                            // 而长间隔又会让界面看着像卡住。
                            val now = System.nanoTime()
                            val elapsedMs = (now - speedSampleStart) / 1_000_000
                            if (done - lastEmit >= 256L * 1024L || elapsedMs >= 500L) {
                                if (elapsedMs > 0) {
                                    bytesPerSecond = speedSampleBytes * 1000L / elapsedMs
                                }
                                lastEmit = done
                                publishProgress(
                                    run,
                                    ApkUpdateState.Downloading(
                                        progress = if (total > 0) (done.toFloat() / total).coerceIn(0f, 1f) else 0f,
                                        downloadedBytes = done,
                                        totalBytes = total,
                                        attempt = attempt,
                                        bytesPerSecond = bytesPerSecond,
                                    ),
                                )
                            }
                        }
                    }
                    // 分片收尾立刻推一次，保证最后一段的进度不被节流吞掉。
                    publishProgress(
                        run,
                        ApkUpdateState.Downloading(
                            progress = if (total > 0) (done.toFloat() / total).coerceIn(0f, 1f) else 0f,
                            downloadedBytes = done,
                            totalBytes = total,
                            attempt = attempt,
                            bytesPerSecond = bytesPerSecond,
                        ),
                    )
                    if (source.sizeBytes > 0) {
                        check(segment == source.sizeBytes) { "第 ${index + 1} 段大小不匹配" }
                    }
                    if (digest != null) {
                        check(digest.digest().toHex().equals(source.sha256, ignoreCase = true)) {
                            "第 ${index + 1} 段 SHA-256 校验失败"
                        }
                    }
                }
            }
        }
        if (artifact.sizeBytes > 0) check(done == artifact.sizeBytes) { "文件大小不匹配" }
    }

    /** 解析 `apk.parts`；任一段地址或摘要不合法即拒绝整份清单（不回退到单对象）。 */
    private fun parseParts(array: JSONArray?): List<ApkPart> {
        if (array == null) return emptyList()
        return buildList {
            for (index in 0 until array.length()) {
                val item = array.optJSONObject(index) ?: continue
                val partUrl = item.optString("url").trim()
                val partSha256 = item.optString("sha256").trim().lowercase(Locale.US)
                require(partUrl.isNotBlank() && Uri.parse(partUrl).scheme == "https") {
                    "APK 分片地址必须使用 HTTPS"
                }
                require(partSha256.matches(Regex("[0-9a-f]{64}"))) { "APK 分片 SHA-256 无效" }
                add(
                    ApkPart(
                        url = partUrl,
                        sizeBytes = item.optLong("size", 0),
                        sha256 = partSha256,
                    ),
                )
            }
        }
    }

    /** 整包摘要校验：失败多半是传输损坏，仍交给下载重试兜底。 */
    private fun verifySha256(file: File, manifest: ApkUpdateManifest, run: DownloadRun) {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                ensureNotCancelled(run)
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        val actualSha = digest.digest().toHex()
        check(actualSha.equals(manifest.apk.sha256, ignoreCase = true)) { "SHA-256 校验失败" }
    }

    /**
     * 包名/签名校验。整包摘要已对上后，这几项失败是确定性的：重下同一个包也不会
     * 变好，抛 [PackageValidationError] 让重试循环立即收手，直接把原因告诉用户。
     *
     * 签名读取双通道：API 28+ 的 signingInfo 在部分投影仪定制 ROM 的包解析器上
     * 可能取不到，退回老 GET_SIGNATURES 通道；发布包自 v1.39 起 v1+v2 双签，
     * 两条通道至少一条能读出证书，不再依赖单一解析路径。
     */
    @Suppress("DEPRECATION")
    private fun validatePackage(file: File, manifest: ApkUpdateManifest) {
        val modern = appContext.packageManager.getPackageArchiveInfo(
            file.absolutePath,
            PackageManager.GET_SIGNING_CERTIFICATES,
        )
        val legacy = appContext.packageManager.getPackageArchiveInfo(
            file.absolutePath,
            PackageManager.GET_SIGNATURES,
        )
        val archive = modern ?: legacy
            ?: throw PackageValidationError("APK 解析失败（设备系统无法读取安装包）")
        if (archive.packageName != appContext.packageName) {
            throw PackageValidationError("APK 包名不匹配")
        }
        val expected = manifest.apk.certificateSha256
        if (expected.isEmpty()) return
        val signerBytes = modern?.signingInfo?.apkContentsSigners
            ?.map { it.toByteArray() }
            ?.takeIf { it.isNotEmpty() }
            ?: legacy?.signatures
                ?.map { it.toByteArray() }
                ?.takeIf { it.isNotEmpty() }
            ?: throw PackageValidationError("APK 签名信息缺失（设备系统兼容问题）")
        val actual = signerBytes
            .map { MessageDigest.getInstance("SHA-256").digest(it).toHex().uppercase(Locale.US) }
        if (actual.none { it in expected }) {
            throw PackageValidationError("APK 签名证书不匹配")
        }
    }

    /** 确定性校验失败：重下同一文件也不会变好，直接报错、不再烧流量重试。 */
    private class PackageValidationError(message: String) : IllegalStateException(message)

    private fun launchInstaller(apk: File) {
        // 安装链路三级降级：ACTION_VIEW（多数 ROM 的安装器都会处理）→
        // PackageInstaller 会话（没有 mime 处理器的 ROM）→ 权限提示/错误。
        if (tryActionViewInstall(apk)) return
        if (trySessionInstall(apk)) return
        if (!canInstallUnknownApps()) {
            _state.value = ApkUpdateState.PermissionRequired
            return
        }
        _state.value = ApkUpdateState.Error(
            "无法调起系统安装器，请稍后重试或手动安装下载的更新包",
        )
    }

    private fun tryActionViewInstall(apk: File): Boolean = runCatching {
            val uri = FileProvider.getUriForFile(
                appContext,
                "${appContext.packageName}.fileprovider",
                apk,
            )
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            appContext.startActivity(intent)
            true
    }.onFailure {
        OtvLog.w("apk update: ACTION_VIEW 调起安装器失败: ${it.message}")
    }.getOrDefault(false)

    private fun trySessionInstall(apk: File): Boolean = runCatching {
        val installer = appContext.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
        val sessionId = installer.createSession(params)
        installer.openSession(sessionId).use { session ->
            apk.inputStream().use { input ->
                session.openWrite("base", 0, apk.length()).use { output ->
                    input.copyTo(output)
                    session.fsync(output)
                }
            }
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0
            val callback = PendingIntent.getBroadcast(
                appContext,
                sessionId,
                Intent(appContext, ApkInstallStatusReceiver::class.java),
                flags,
            )
            session.commit(callback.intentSender)
        }
        true
    }.onFailure {
        OtvLog.w("apk update: PackageInstaller 会话调起失败: ${it.message}")
    }.getOrDefault(false)

    /** PackageInstaller 会话结果回调（由 [ApkInstallStatusReceiver] 转发）。 */
    internal fun onInstallSessionStatus(status: Int, message: String?, userAction: Intent?) {
        when (status) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                if (userAction == null) return
                runCatching {
                    appContext.startActivity(userAction.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                }.onFailure { OtvLog.w("apk update: 打开安装确认页失败: ${it.message}") }
            }
            PackageInstaller.STATUS_SUCCESS -> _state.value = null
            else -> {
                OtvLog.w("apk update: 会话安装未完成 status=$status message=$message")
                _state.value = ApkUpdateState.Error(
                    message ?: "系统安装未完成，请稍后重试或手动安装下载的更新包",
                )
            }
        }
    }

    private fun canInstallUnknownApps(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.O ||
            appContext.packageManager.canRequestPackageInstalls()

    private fun isDismissed(versionCode: Long): Boolean =
        appContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getLong(DISMISSED_VERSION, 0L) >= versionCode

    private fun JSONArray?.toStringList(): List<String> = buildList {
        if (this@toStringList == null) return@buildList
        for (index in 0 until length()) {
            optString(index).takeIf { it.isNotBlank() }?.let(::add)
        }
    }

    private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }
}

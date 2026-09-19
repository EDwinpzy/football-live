package com.qiubo.optimaltv.apkupdate

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import com.qiubo.optimaltv.BuildConfig
import com.qiubo.optimaltv.OtvLog
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
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
    ) : ApkUpdateState

    data object PermissionRequired : ApkUpdateState
    data object Installing : ApkUpdateState
    data class Error(val message: String) : ApkUpdateState
}

data class ApkUpdateOffer(
    val manifest: ApkUpdateManifest,
    val decision: ApkUpdateDecision,
) {
    val required: Boolean get() = decision == ApkUpdateDecision.REQUIRED
}

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
    private val http = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(90, TimeUnit.SECONDS)
        .build()

    private lateinit var appContext: Context
    private var initialized = false
    private var downloadRunning = false

    private val _offer = MutableStateFlow<ApkUpdateOffer?>(null)
    val offer: StateFlow<ApkUpdateOffer?> = _offer.asStateFlow()

    private val _state = MutableStateFlow<ApkUpdateState?>(null)
    val state: StateFlow<ApkUpdateState?> = _state.asStateFlow()

    fun init(context: Context) {
        if (initialized || !BuildConfig.APK_UPDATE_ENABLED) return
        initialized = true
        appContext = context.applicationContext
        scope.launch {
            runCatching { checkLatest() }
                .onFailure { OtvLog.w("apk update: 检查失败（静默跳过）: ${it.message}") }
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
        if (current.required) return
        appContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putLong(DISMISSED_VERSION, current.manifest.versionCode)
            .apply()
        _offer.value = null
        _state.value = null
    }

    fun acceptUpdate() {
        val current = _offer.value ?: return
        if (downloadRunning) return
        if (!canInstallUnknownApps()) {
            _state.value = ApkUpdateState.PermissionRequired
            openUnknownAppSettings()
            return
        }
        downloadRunning = true
        _state.value = ApkUpdateState.Downloading(0f, 0L, current.manifest.apk.sizeBytes)
        scope.launch {
            try {
                val apk = downloadAndVerify(current.manifest)
                _state.value = ApkUpdateState.Installing
                withContext(Dispatchers.Main) { launchInstaller(apk) }
            } catch (t: Throwable) {
                OtvLog.w("apk update: 下载或校验失败: ${t.message}")
                _state.value = ApkUpdateState.Error(t.message ?: "更新失败，请稍后重试")
            } finally {
                downloadRunning = false
            }
        }
    }

    fun retryUpdate() {
        _state.value = null
        acceptUpdate()
    }

    fun openUnknownAppSettings() {
        if (!initialized) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val intent = Intent(
                Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:${appContext.packageName}"),
            ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            appContext.startActivity(intent)
        } else {
            appContext.startActivity(
                Intent(Settings.ACTION_SECURITY_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
        }
    }

    private suspend fun checkLatest() {
        val endpoint = BuildConfig.APK_UPDATE_URL.trim()
        if (endpoint.isBlank()) return
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
            if (manifest.packageName != appContext.packageName) return
            val localVersion = appContext.packageManager
                .getPackageInfo(appContext.packageName, 0).longVersionCode
            val decision = ApkUpdatePolicy.decide(localVersion, manifest)
            if (decision == ApkUpdateDecision.NONE) return
            if (decision == ApkUpdateDecision.OPTIONAL && isDismissed(manifest.versionCode)) return
            _offer.value = ApkUpdateOffer(manifest, decision)
        }
    }

    private fun parseManifest(root: JSONObject): ApkUpdateManifest {
        val apk = root.optJSONObject("apk") ?: root
        val url = apk.optString("url").trim()
        val sha256 = apk.optString("sha256").trim().lowercase(Locale.US)
        require(url.isNotBlank() && Uri.parse(url).scheme == "https") { "APK 地址必须使用 HTTPS" }
        require(sha256.matches(Regex("[0-9a-f]{64}"))) { "APK SHA-256 无效" }
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
                sizeBytes = apk.optLong("size", 0),
                sha256 = sha256,
                certificateSha256 = apk.optJSONArray("certificateSha256").toStringList()
                    .map { it.uppercase(Locale.US) },
            ),
        ).also {
            require(it.versionCode > 0) { "APK versionCode 无效" }
            require(it.versionCode >= it.minSupportedVersionCode) { "最低支持版本不能高于发布版本" }
            require(it.packageName.isNotBlank()) { "packageName 为空" }
        }
    }

    private suspend fun downloadAndVerify(manifest: ApkUpdateManifest): File {
        val dir = File(appContext.filesDir, "update").apply { mkdirs() }
        val safeName = manifest.apk.fileName.substringAfterLast('/').ifBlank { "update.apk" }
        val part = File(dir, "$safeName.part")
        val target = File(dir, safeName)
        var lastError: Throwable? = null
        repeat(MAX_ATTEMPTS) { attempt ->
            try {
                downloadOnce(manifest.apk, part)
                verifyArtifact(part, manifest)
                if (target.exists()) target.delete()
                check(part.renameTo(target)) { "无法保存更新文件" }
                return target
            } catch (t: Throwable) {
                lastError = t
                part.delete()
                if (attempt + 1 < MAX_ATTEMPTS) delay((attempt + 1) * 1500L)
            }
        }
        throw lastError ?: IllegalStateException("下载失败")
    }

    private fun downloadOnce(artifact: ApkArtifact, part: File) {
        val request = Request.Builder().url(artifact.url).build()
        http.newCall(request).execute().use { response ->
            check(response.isSuccessful) { "下载 HTTP ${response.code}" }
            val body = requireNotNull(response.body) { "下载响应为空" }
            val total = if (artifact.sizeBytes > 0) artifact.sizeBytes else body.contentLength()
            var done = 0L
            body.byteStream().use { input ->
                part.outputStream().use { output ->
                    val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        output.write(buffer, 0, count)
                        done += count
                        _state.value = ApkUpdateState.Downloading(
                            progress = if (total > 0) (done.toFloat() / total).coerceIn(0f, 1f) else 0f,
                            downloadedBytes = done,
                            totalBytes = total,
                        )
                    }
                }
            }
            if (artifact.sizeBytes > 0) check(done == artifact.sizeBytes) { "文件大小不匹配" }
        }
    }

    private fun verifyArtifact(file: File, manifest: ApkUpdateManifest) {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        val actualSha = digest.digest().toHex()
        check(actualSha.equals(manifest.apk.sha256, ignoreCase = true)) { "SHA-256 校验失败" }

        val archive = appContext.packageManager.getPackageArchiveInfo(
            file.absolutePath,
            PackageManager.GET_SIGNING_CERTIFICATES,
        ) ?: error("APK 解析失败")
        check(archive.packageName == appContext.packageName) { "APK 包名不匹配" }
        val expected = manifest.apk.certificateSha256
        if (expected.isNotEmpty()) {
            val signingInfo = archive.signingInfo ?: error("APK 签名信息缺失")
            val actual = signingInfo.apkContentsSigners
                .map { it.toByteArray() }
                .map { MessageDigest.getInstance("SHA-256").digest(it).toHex().uppercase(Locale.US) }
            check(actual.any { it in expected }) { "APK 签名证书不匹配" }
        }
    }

    private fun launchInstaller(apk: File) {
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

package com.qiubo.optimaltv.vpn

import android.content.Context
import android.os.SystemClock
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URI
import java.net.URL
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.concurrent.TimeUnit

/** Starts the bundled Mihomo HTTP proxy used only by Android football requests. */
object FootballVpnProxyCore {
    const val PROXY_URL = "http://127.0.0.1:17890"

    private const val LISTEN_PORT = 17890
    private const val START_TIMEOUT_MS = 25_000L
    private const val MAX_CONFIG_RESPONSE_BYTES = 64 * 1024
    private const val REMOTE_CONNECT_TIMEOUT_MS = 2_000
    private const val REMOTE_READ_TIMEOUT_MS = 2_000
    private const val REMOTE_FAILURE_RETRY_MS = 30_000L
    private const val CONFIG_NAME = "football-mihomo.yaml"
    private const val SUBSCRIPTIONS_NAME = "football-vpn-subscriptions.json"
    private val lock = Any()
    private val refreshLock = Any()
    @Volatile private var process: Process? = null
    @Volatile private var lastFailedRefreshAtMs = -1L

    internal data class SubscriptionConfig(
        val version: String,
        val updatedAt: String,
        val subscriptions: List<String>,
    )

    /**
     * Refreshes the app-managed subscriptions from the public business API. No response body,
     * subscription URL, or request URL is logged: subscription URLs contain credentials.
     * A failed request leaves the last valid local configuration untouched.
     */
    fun refreshSubscriptions(context: Context, apiBaseUrl: String): Boolean {
        if (apiBaseUrl.isBlank()) return false
        return synchronized(refreshLock) {
            val now = SystemClock.elapsedRealtime()
            if (lastFailedRefreshAtMs >= 0L && now - lastFailedRefreshAtMs < REMOTE_FAILURE_RETRY_MS) {
                return@synchronized false
            }
            val config = fetchRemoteConfig(apiBaseUrl.trimEnd('/') + "/football-vpn/config")
            if (config == null) {
                lastFailedRefreshAtMs = SystemClock.elapsedRealtime()
                return@synchronized false
            }
            lastFailedRefreshAtMs = -1L
            applyRemoteConfig(context.applicationContext, config)
        }
    }

    private fun applyRemoteConfig(appContext: Context, config: SubscriptionConfig): Boolean = synchronized(lock) {
        val home = File(appContext.filesDir, "football-vpn").apply { mkdirs() }
        val configFile = File(home, CONFIG_NAME)
        val template = readValidLocalConfig(configFile) ?: readBundledConfig(appContext) ?: return@synchronized false
        val updatedYaml = replaceSubscriptionUrls(template, config.subscriptions) ?: return@synchronized false
        val providerUrlsChanged = extractSubscriptionUrls(template) != config.subscriptions

        val previous = readSubscriptionConfig(File(home, SUBSCRIPTIONS_NAME))
        val changed = previous?.let {
            it.version != config.version || it.subscriptions != config.subscriptions
        } ?: true
        if (!changed) {
            // Repair a missing or stale YAML from the last known-good snapshot if needed.
            val currentYaml = readValidLocalConfig(configFile)
            if (currentYaml == null || currentYaml != updatedYaml) {
                if (!writeAtomically(configFile, updatedYaml)) return@synchronized false
                if (providerUrlsChanged) clearProviderCache(home)
                if (process?.isAlive == true) {
                    stopProcessLocked()
                    ensureReadyLocked(appContext)
                }
            }
            return@synchronized false
        }

        // Keep a durable source of truth as well as the runtime YAML. If the process dies
        // between these writes, ensureConfigFileLocked reconstructs the YAML from this cache.
        if (!writeAtomically(File(home, SUBSCRIPTIONS_NAME), config.toJson().toString())) {
            return@synchronized false
        }
        if (!writeAtomically(configFile, updatedYaml)) return@synchronized false
        if (providerUrlsChanged) clearProviderCache(home)

        val wasRunning = process?.isAlive == true
        if (wasRunning) {
            stopProcessLocked()
            // Applying a new version immediately ensures the next football line resolution
            // uses it; existing football playback also shares this local proxy process.
            ensureReadyLocked(appContext)
        }
        true
    }

    fun ensureReady(context: Context): Boolean = synchronized(lock) {
        ensureReadyLocked(context.applicationContext)
    }

    private fun ensureReadyLocked(context: Context): Boolean {
        if (isListening()) return true
        process?.takeIf { it.isAlive }?.let { running ->
            if (waitForListener(running)) return true
            running.destroyForcibly()
            process = null
        }

        val home = File(context.filesDir, "football-vpn").apply { mkdirs() }
        if (!ensureConfigFileLocked(context, home)) return false

        // Android 14 blocks executing newly written files from app data. Gradle
        // packages one immutable executable per ABI under lib/ via jniLibs.
        val core = File(context.applicationInfo.nativeLibraryDir, "libmihomo.so")
        if (!core.isFile || !core.canExecute()) return false

        val config = File(home, CONFIG_NAME)
        File(home, "providers").mkdirs()
        val child = try {
            ProcessBuilder(core.absolutePath, "-d", home.absolutePath, "-f", config.absolutePath)
                .directory(home)
                .redirectErrorStream(true)
                .start()
        } catch (_: Exception) {
            return false
        }
        Thread({
            runCatching {
                child.inputStream.use { input ->
                    val buffer = ByteArray(4096)
                    while (input.read(buffer) >= 0) Unit
                }
            }
        }, "football-vpn-core-output").apply {
            isDaemon = true
            start()
        }
        process = child
        return waitForListener(child)
    }

    private fun ensureConfigFileLocked(context: Context, home: File): Boolean {
        val configFile = File(home, CONFIG_NAME)
        val cached = readSubscriptionConfig(File(home, SUBSCRIPTIONS_NAME))
        val current = readValidLocalConfig(configFile)
        if (current != null) {
            if (cached == null || extractSubscriptionUrls(current) == cached.subscriptions) return true
            val refreshed = replaceSubscriptionUrls(current, cached.subscriptions) ?: return false
            return writeAtomically(configFile, refreshed)
        }

        val bundled = readBundledConfig(context) ?: return false
        val initial = if (cached == null) bundled else replaceSubscriptionUrls(bundled, cached.subscriptions) ?: return false
        return writeAtomically(configFile, initial)
    }

    private fun fetchRemoteConfig(endpoint: String): SubscriptionConfig? {
        var connection: HttpURLConnection? = null
        return try {
            connection = URL(endpoint).openConnection() as? HttpURLConnection ?: return null
            connection.requestMethod = "GET"
            connection.connectTimeout = REMOTE_CONNECT_TIMEOUT_MS
            connection.readTimeout = REMOTE_READ_TIMEOUT_MS
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("User-Agent", "MyTV-Android/1.0")
            if (connection.responseCode !in 200..299) return null
            val body = connection.inputStream.bufferedReader(Charsets.UTF_8).use { reader ->
                val output = StringBuilder()
                val buffer = CharArray(4096)
                var total = 0
                while (true) {
                    val count = reader.read(buffer)
                    if (count < 0) break
                    total += count
                    if (total > MAX_CONFIG_RESPONSE_BYTES) return null
                    output.append(buffer, 0, count)
                }
                output.toString()
            }
            parseRemoteConfig(body)
        } catch (_: Exception) {
            null
        } finally {
            connection?.disconnect()
        }
    }

    internal fun parseRemoteConfig(json: String): SubscriptionConfig? {
        return try {
            val response = JSONObject(json)
            if (response.optInt("ret", -1) != 0) return null
            val version = response.optString("version").trim()
            val timestamp = response.opt("updatedAt")
            val updatedAt = if (timestamp == null || timestamp === JSONObject.NULL) "" else timestamp.toString().trim()
            val array = response.optJSONArray("subscriptions") ?: return null
            if (!hasValidTimestamp(version, updatedAt) || array.length() != 3) return null
            val urls = (0 until array.length()).map { index -> array.getString(index).trim() }
            if (urls.any { !isValidSubscriptionUrl(it) }) return null
            SubscriptionConfig(version, updatedAt, urls)
        } catch (_: Exception) {
            null
        }
    }

    private fun hasValidTimestamp(version: String, updatedAt: String): Boolean =
        version.isNotBlank() && (version.toLongOrNull() == 0L || updatedAt.isNotBlank())

    internal fun isValidSubscriptionUrl(value: String): Boolean {
        if (value.isBlank() || value.length > 4096 || value.any { it.isWhitespace() || it.isISOControl() }) return false
        if ('"' in value || '\\' in value) return false
        return runCatching {
            val uri = URI(value)
            uri.isAbsolute && uri.scheme.equals("https", ignoreCase = true) && !uri.host.isNullOrBlank()
        }.getOrDefault(false)
    }

    /** Replaces only the three subscription provider URL fields, leaving all bundled rules intact. */
    internal fun replaceSubscriptionUrls(yaml: String, subscriptions: List<String>): String? {
        if (subscriptions.size != 3 || subscriptions.any { !isValidSubscriptionUrl(it) }) return null
        val lines = yaml.split('\n').toMutableList()
        val replaced = BooleanArray(3)
        var inProviders = false
        var currentProvider = -1
        val provider = Regex("^  subscription_([1-3]):\\s*$")
        val url = Regex("^(    url:\\s*)\"[^\"]*\"([ \\t]*(?:#.*)?)$")

        for (index in lines.indices) {
            val line = lines[index].removeSuffix("\r")
            if (line == "proxy-providers:") {
                inProviders = true
                currentProvider = -1
                continue
            }
            if (inProviders && line.isNotBlank() && !line.startsWith(" ")) {
                inProviders = false
                currentProvider = -1
            }
            if (!inProviders) continue

            val providerMatch = provider.matchEntire(line)
            if (providerMatch != null) {
                currentProvider = providerMatch.groupValues[1].toInt() - 1
                continue
            }
            if (line.startsWith("  ") && !line.startsWith("    ")) {
                currentProvider = -1
                continue
            }
            if (currentProvider !in 0..2 || replaced[currentProvider]) continue
            val urlMatch = url.matchEntire(line) ?: continue
            lines[index] = urlMatch.groupValues[1] + "\"" + subscriptions[currentProvider] + "\"" + urlMatch.groupValues[2]
            replaced[currentProvider] = true
        }
        if (!replaced.all { it }) return null
        return lines.joinToString("\n")
    }

    private fun extractSubscriptionUrls(yaml: String): List<String>? {
        val values = arrayOfNulls<String>(3)
        var inProviders = false
        var currentProvider = -1
        val provider = Regex("^  subscription_([1-3]):\\s*$")
        val url = Regex("^    url:\\s*\"([^\"]+)\"(?:[ \\t]*#.*)?$")
        yaml.lineSequence().forEach { rawLine ->
            val line = rawLine.removeSuffix("\r")
            if (line == "proxy-providers:") {
                inProviders = true
                currentProvider = -1
                return@forEach
            }
            if (inProviders && line.isNotBlank() && !line.startsWith(" ")) {
                inProviders = false
                currentProvider = -1
            }
            if (!inProviders) return@forEach
            val providerMatch = provider.matchEntire(line)
            if (providerMatch != null) {
                currentProvider = providerMatch.groupValues[1].toInt() - 1
                return@forEach
            }
            if (line.startsWith("  ") && !line.startsWith("    ")) currentProvider = -1
            if (currentProvider !in 0..2) return@forEach
            val match = url.matchEntire(line) ?: return@forEach
            values[currentProvider] = match.groupValues[1]
        }
        val result = values.map { it ?: return null }
        return result.takeIf { urls -> urls.all(::isValidSubscriptionUrl) }
    }

    private fun readValidLocalConfig(file: File): String? {
        return try {
            if (!file.isFile) return null
            val yaml = file.readText(Charsets.UTF_8)
            if (extractSubscriptionUrls(yaml) == null) null else yaml
        } catch (_: Exception) {
            null
        }
    }

    private fun readBundledConfig(context: Context): String? = try {
        val yaml = context.assets.open(CONFIG_NAME).bufferedReader(Charsets.UTF_8).use { it.readText() }
        if (extractSubscriptionUrls(yaml) == null) null else yaml
    } catch (_: Exception) {
        null
    }

    private fun readSubscriptionConfig(file: File): SubscriptionConfig? {
        return try {
            if (!file.isFile || file.length() > MAX_CONFIG_RESPONSE_BYTES) return null
            val json = JSONObject(file.readText(Charsets.UTF_8))
            val version = json.optString("version").trim()
            val timestamp = json.opt("updatedAt")
            val updatedAt = if (timestamp == null || timestamp === JSONObject.NULL) "" else timestamp.toString().trim()
            val array = json.optJSONArray("subscriptions") ?: return null
            if (!hasValidTimestamp(version, updatedAt) || array.length() != 3) return null
            val urls = (0 until array.length()).map { array.getString(it).trim() }
            if (urls.any { !isValidSubscriptionUrl(it) }) return null
            SubscriptionConfig(version, updatedAt, urls)
        } catch (_: Exception) {
            null
        }
    }

    private fun SubscriptionConfig.toJson(): JSONObject = JSONObject()
        .put("version", version)
        .put("updatedAt", updatedAt)
        .put("subscriptions", org.json.JSONArray(subscriptions))

    private fun writeAtomically(file: File, text: String): Boolean = try {
        file.parentFile?.mkdirs()
        val temporary = File(file.parentFile, file.name + ".tmp")
        FileOutputStream(temporary).use { output ->
            output.write(text.toByteArray(Charsets.UTF_8))
            output.fd.sync()
        }
        try {
            Files.move(
                temporary.toPath(),
                file.toPath(),
                StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING,
            )
        } catch (_: AtomicMoveNotSupportedException) {
            Files.move(temporary.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING)
        }
        true
    } catch (_: Exception) {
        false
    }

    private fun stopProcessLocked() {
        val running = process
        process = null
        if (running == null) return
        try {
            running.destroy()
            if (!running.waitFor(1_500, TimeUnit.MILLISECONDS)) {
                running.destroyForcibly()
                running.waitFor(1_500, TimeUnit.MILLISECONDS)
            }
        } catch (_: Exception) {
            runCatching { running.destroyForcibly() }
        }
        val deadline = SystemClock.elapsedRealtime() + 2_000L
        while (SystemClock.elapsedRealtime() < deadline && isListening()) {
            SystemClock.sleep(50)
        }
    }

    private fun clearProviderCache(home: File) {
        val providers = File(home, "providers")
        listOf("subscription_1.yaml", "subscription_2.yaml", "subscription_3.yaml")
            .forEach { File(providers, it).delete() }
    }

    private fun waitForListener(child: Process): Boolean {
        val deadline = SystemClock.elapsedRealtime() + START_TIMEOUT_MS
        while (SystemClock.elapsedRealtime() < deadline) {
            if (!child.isAlive) return false
            if (isListening()) return true
            SystemClock.sleep(100)
        }
        return isListening()
    }

    private fun isListening(): Boolean = try {
        Socket().use { socket ->
            socket.connect(InetSocketAddress("127.0.0.1", LISTEN_PORT), 250)
        }
        true
    } catch (_: Exception) {
        false
    }
}

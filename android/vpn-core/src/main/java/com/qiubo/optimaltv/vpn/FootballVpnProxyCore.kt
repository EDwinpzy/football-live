package com.qiubo.optimaltv.vpn

import android.content.Context
import android.os.SystemClock
import android.util.Log
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URI
import java.net.URLEncoder
import java.net.URL
import java.nio.charset.StandardCharsets
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.SecureRandom
import java.util.concurrent.ExecutorCompletionService
import java.util.concurrent.ExecutionException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.FutureTask
import java.util.concurrent.Callable
import java.util.concurrent.TimeoutException
import java.util.concurrent.TimeUnit
import java.util.concurrent.CancellationException
import java.util.concurrent.ThreadFactory
import java.util.concurrent.atomic.AtomicInteger

/** Starts the bundled Mihomo HTTP proxy used only by Android football requests. */
object FootballVpnProxyCore {
    const val PROXY_URL = "http://127.0.0.1:17890"

    private const val LISTEN_PORT = 17890
    private const val CONTROLLER_PORT = 17891
    private const val CONTROLLER_URL = "http://127.0.0.1:$CONTROLLER_PORT"
    private const val PROXY_GROUP_NAME = "FOOTBALL-NODES"
    private const val DEFAULT_MATCH_PROBE_URL = "https://www.88zhibo.tv/"
    private const val START_TIMEOUT_MS = 25_000L
    private const val MAX_CONFIG_RESPONSE_BYTES = 64 * 1024
    private const val MAX_CONTROLLER_RESPONSE_BYTES = 4 * 1024 * 1024
    private const val REMOTE_CONNECT_TIMEOUT_MS = 2_000
    private const val REMOTE_READ_TIMEOUT_MS = 2_000
    private const val CONTROLLER_CONNECT_TIMEOUT_MS = 3_000
    private const val CONTROLLER_READ_TIMEOUT_MS = 8_000
    private const val NODE_DELAY_TIMEOUT_MS = 5_000
    private const val NODE_TEST_PARALLELISM = 12
    private const val PROVIDER_WAIT_ATTEMPTS = 16
    private const val REMOTE_FAILURE_RETRY_MS = 30_000L
    private const val CONFIG_NAME = "football-mihomo.yaml"
    private const val SUBSCRIPTIONS_NAME = "football-vpn-subscriptions.json"
    private const val CONTROLLER_SECRET_NAME = "football-mihomo-controller-secret"
    private const val NICE_EXECUTABLE = "/system/bin/nice"
    private const val TAG = "FootballVpnProxy"
    private const val SELECTION_POLL_MS = 100L
    private val lock = Any()
    private val refreshLock = Any()
    private val matchSelectionLock = Any()
    private val matchSelectionExecutor: ExecutorService =
        Executors.newSingleThreadExecutor(backgroundThreadFactory("football-node-selection"))
    @Volatile private var process: Process? = null
    @Volatile private var lastFailedRefreshAtMs = -1L
    private var matchSelectionFlight: MatchSelectionFlight? = null

    private val GROUP_PROXY_TYPES = setOf(
        "selector", "urltest", "url-test", "fallback", "loadbalance", "load-balance", "relay",
    )
    private val RESERVED_PROXY_TYPES = setOf("direct", "reject", "reject-drop", "compatible", "pass", "dns")
    private val RESERVED_PROXY_NAMES = setOf(
        "direct", "reject", "reject-drop", "compatible", "pass", "dns", "global", "block", "block-dns",
    )

    internal data class NodeLatency(
        val name: String,
        val delayMs: Int,
    )

    private class CancellationSignal {
        private val lock = Any()
        private val connections = LinkedHashSet<HttpURLConnection>()
        @Volatile var cancelled: Boolean = false
            private set

        fun track(connection: HttpURLConnection): Boolean = synchronized(lock) {
            if (cancelled) {
                false
            } else {
                connections += connection
                true
            }
        }

        fun untrack(connection: HttpURLConnection) {
            synchronized(lock) { connections.remove(connection) }
        }

        fun cancel() {
            val active = synchronized(lock) {
                cancelled = true
                connections.toList().also { connections.clear() }
            }
            active.forEach { runCatching { it.disconnect() } }
        }
    }

    private class MatchSelectionFlight(
        val key: String,
        val signal: CancellationSignal,
        val task: FutureTask<Boolean>,
        var waiters: Int = 0,
    )

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

    /**
     * Measures every concrete member of the football group and selects the fastest node that
     * can reach the requested HTTPS probe. Simultaneous requests for the same match share work.
     */
    fun prepareForMatch(
        context: Context,
        probeUrl: String = DEFAULT_MATCH_PROBE_URL,
        selectionKey: String = probeUrl,
        isCancelled: () -> Boolean = { false },
    ): Boolean {
        val normalizedProbeUrl = normalizeProbeUrl(probeUrl) ?: return false
        val normalizedKey = selectionKey.trim().takeIf { it.isNotEmpty() } ?: normalizedProbeUrl
        val appContext = context.applicationContext

        val flight = synchronized(matchSelectionLock) {
            matchSelectionFlight?.takeIf { it.task.isDone }?.let { matchSelectionFlight = null }
            val running = matchSelectionFlight
            if (running != null && running.key != normalizedKey) {
                // A new match supersedes an unfinished probe. Cancel its HTTP calls before
                // starting another selection so it cannot later change the shared selector.
                cancelFlightLocked(running)
                matchSelectionFlight = null
            }
            matchSelectionFlight ?: run {
                val signal = CancellationSignal()
                val task = FutureTask<Boolean> { selectNodeForMatch(appContext, normalizedProbeUrl, signal) }
                MatchSelectionFlight(normalizedKey, signal, task).also { created ->
                    matchSelectionFlight = created
                    matchSelectionExecutor.execute(task)
                }
            }.also { it.waiters++ }
        }

        try {
            while (true) {
                checkSelectionCancelled(isCancelled, flight.signal)
                try {
                    return flight.task.get(SELECTION_POLL_MS, TimeUnit.MILLISECONDS)
                } catch (_: TimeoutException) {
                    // Poll the caller's coroutine state while the background network probes run.
                } catch (interrupted: InterruptedException) {
                    Thread.currentThread().interrupt()
                    throw selectionCancelled("football node selection interrupted", interrupted)
                } catch (cancelled: CancellationException) {
                    if (isCancelled() || Thread.currentThread().isInterrupted) throw cancelled
                    return false
                } catch (failed: ExecutionException) {
                    if (isCancelled() || Thread.currentThread().isInterrupted) {
                        throw selectionCancelled("football node selection cancelled", failed)
                    }
                    Log.w(TAG, "node selection failed", failed.cause ?: failed)
                    return false
                }
            }
        } finally {
            synchronized(matchSelectionLock) {
                flight.waiters = (flight.waiters - 1).coerceAtLeast(0)
                if (flight.waiters == 0 && !flight.task.isDone && matchSelectionFlight === flight) {
                    cancelFlightLocked(flight)
                    matchSelectionFlight = null
                } else if (flight.task.isDone && matchSelectionFlight === flight) {
                    matchSelectionFlight = null
                }
            }
        }
    }

    private fun cancelFlightLocked(flight: MatchSelectionFlight) {
        flight.signal.cancel()
        flight.task.cancel(true)
    }

    private fun checkSelectionCancelled(isCancelled: () -> Boolean, signal: CancellationSignal) {
        if (shouldCancelSelection(Thread.currentThread().isInterrupted, isCancelled(), signal.cancelled)) {
            throw CancellationException("football node selection cancelled")
        }
    }

    internal fun shouldCancelSelection(
        threadInterrupted: Boolean,
        callerCancelled: Boolean,
        selectionCancelled: Boolean,
    ): Boolean = threadInterrupted || callerCancelled || selectionCancelled

    private fun selectionCancelled(message: String, cause: Throwable): CancellationException =
        CancellationException(message).apply { initCause(cause) }

    private fun selectNodeForMatch(
        context: Context,
        probeUrl: String,
        signal: CancellationSignal,
    ): Boolean {
        Log.i(TAG, "node selection started")
        checkSelectionCancelled({ false }, signal)
        if (!ensureReady(context)) {
            Log.w(TAG, "node selection failed: candidates=0 available=0 (proxy unavailable)")
            return false
        }
        checkSelectionCancelled({ false }, signal)
        val home = File(context.filesDir, "football-vpn")
        val secret = readControllerSecret(File(home, CONTROLLER_SECRET_NAME))
        if (secret == null) {
            Log.w(TAG, "node selection failed: candidates=0 available=0 (controller key unavailable)")
            return false
        }
        var proxyResponse = controllerRequest(secret, "GET", "/proxies", cancellation = signal)
        var providerResponse = controllerRequest(secret, "GET", "/providers/proxies", cancellation = signal)
        var candidates = proxyResponse?.let { parseProxyCandidates(it.body, providerResponse?.body) }
        for (attempt in 0 until PROVIDER_WAIT_ATTEMPTS) {
            if (!candidates.isNullOrEmpty()) break
            checkSelectionCancelled({ false }, signal)
            try {
                Thread.sleep(500)
            } catch (interrupted: InterruptedException) {
                Thread.currentThread().interrupt()
                throw interrupted
            }
            proxyResponse = controllerRequest(secret, "GET", "/proxies", cancellation = signal)
            providerResponse = controllerRequest(secret, "GET", "/providers/proxies", cancellation = signal)
            candidates = proxyResponse?.let { parseProxyCandidates(it.body, providerResponse?.body) }
        }
        checkSelectionCancelled({ false }, signal)
        if (candidates == null || candidates.isEmpty()) {
            Log.w(TAG, "node selection failed: candidates=0 available=0 (no proxy nodes)")
            return false
        }

        val workers = Executors.newFixedThreadPool(
            minOf(NODE_TEST_PARALLELISM, candidates.size),
            backgroundThreadFactory("football-node-probe", daemon = false),
        )
        val providersByNode = providerResponse?.let { parseProviderNodes(it.body) }.orEmpty()
        val completion = ExecutorCompletionService<NodeLatency?>(workers)
        try {
            candidates.forEach { node ->
                completion.submit(Callable {
                    checkSelectionCancelled({ false }, signal)
                    val delay = requestNodeDelay(secret, node, probeUrl, signal, providersByNode[node]?.first)
                    if (delay == null) null else NodeLatency(node, delay)
                })
            }
            val available = ArrayList<NodeLatency>()
            repeat(candidates.size) {
                checkSelectionCancelled({ false }, signal)
                val measured = completion.take().get()
                if (measured != null && measured.delayMs > 0) available += measured
            }
            checkSelectionCancelled({ false }, signal)
            val fastest = selectLowestLatency(available)
            Log.i(
                TAG,
                "node selection: candidates=${candidates.size} available=${available.size} " +
                    "selectedDelayMs=${fastest?.delayMs ?: 0}",
            )
            if (fastest == null) return false

            checkSelectionCancelled({ false }, signal)
            val body = JSONObject().put("name", fastest.name).toString()
            val selected = controllerRequest(
                secret = secret,
                method = "PUT",
                path = "/proxies/${encodePathSegment(PROXY_GROUP_NAME)}",
                body = body,
                cancellation = signal,
            )
            checkSelectionCancelled({ false }, signal)
            return (selected?.statusCode ?: 0) in 200..299
        } catch (interrupted: InterruptedException) {
            Thread.currentThread().interrupt()
            throw interrupted
        } finally {
            workers.shutdownNow()
            runCatching { workers.awaitTermination(1, TimeUnit.SECONDS) }
        }
    }

    private fun requestNodeDelay(
        secret: String,
        node: String,
        probeUrl: String,
        signal: CancellationSignal,
        provider: String?,
    ): Int? {
        val query = "url=${encodeQueryParameter(probeUrl)}&timeout=$NODE_DELAY_TIMEOUT_MS&expected=200"
        val response = controllerRequest(
            secret = secret,
            method = "GET",
            path = nodeDelayPath(node, provider) + "?$query",
            cancellation = signal,
        ) ?: return null
        checkSelectionCancelled({ false }, signal)
        if (response.statusCode !in 200..299) return null
        return parseDelayResponse(response.body)
    }

    private data class ControllerResponse(
        val statusCode: Int,
        val body: String,
    )

    private fun controllerRequest(
        secret: String,
        method: String,
        path: String,
        body: String? = null,
        cancellation: CancellationSignal? = null,
    ): ControllerResponse? {
        var connection: HttpURLConnection? = null
        return try {
            if (cancellation?.cancelled == true || Thread.currentThread().isInterrupted) return null
            connection = URL(CONTROLLER_URL + path).openConnection() as? HttpURLConnection ?: return null
            if (cancellation?.track(connection) == false) return null
            connection.requestMethod = method
            connection.connectTimeout = CONTROLLER_CONNECT_TIMEOUT_MS
            connection.readTimeout = CONTROLLER_READ_TIMEOUT_MS
            connection.setRequestProperty("Authorization", "Bearer $secret")
            connection.setRequestProperty("Accept", "application/json")
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                connection.outputStream.use { it.write(body.toByteArray(StandardCharsets.UTF_8)) }
            }
            val status = connection.responseCode
            if (status !in 200..299) return null
            val responseBody = if (method == "GET") {
                connection.inputStream.use { input ->
                    val output = java.io.ByteArrayOutputStream()
                    val buffer = ByteArray(8192)
                    var total = 0
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        total += count
                        if (total > MAX_CONTROLLER_RESPONSE_BYTES) return null
                        output.write(buffer, 0, count)
                    }
                    output.toString("UTF-8")
                }
            } else {
                ""
            }
            ControllerResponse(status, responseBody)
        } catch (_: Exception) {
            null
        } finally {
            connection?.let { cancellation?.untrack(it) }
            connection?.disconnect()
        }
    }

    internal fun parseProviderNodes(json: String): Map<String, Pair<String, String>> = runCatching {
        val providers = JSONObject(json).optJSONObject("providers") ?: return@runCatching emptyMap()
        val result = LinkedHashMap<String, Pair<String, String>>()
        val names = providers.keys()
        while (names.hasNext()) {
            val provider = names.next()
            val nodes = providers.optJSONObject(provider)?.optJSONArray("proxies") ?: continue
            for (index in 0 until nodes.length()) {
                val node = nodes.optJSONObject(index) ?: continue
                val name = node.optString("name").trim()
                val type = node.optString("type").trim().lowercase()
                if (name.isNotEmpty() && !result.containsKey(name)) result[name] = provider to type
            }
        }
        result.toMap()
    }.getOrDefault(emptyMap())

    internal fun nodeDelayPath(node: String, provider: String?): String =
        if (provider.isNullOrBlank()) "/proxies/${encodePathSegment(node)}/delay"
        else "/providers/proxies/${encodePathSegment(provider)}/${encodePathSegment(node)}/healthcheck"

    internal fun parseProxyCandidates(json: String, providersJson: String? = null): List<String>? {
        return try {
            val proxies = JSONObject(json).optJSONObject("proxies") ?: return null
            val group = proxies.optJSONObject(PROXY_GROUP_NAME) ?: return null
            val members = group.optJSONArray("all") ?: return null
            val providerNodes = providersJson?.let { parseProviderNodes(it) }.orEmpty()
            val candidates = LinkedHashSet<String>()
            for (index in 0 until members.length()) {
                val name = members.optString(index).trim()
                if (name.isEmpty() || isReservedProxyName(name)) continue
                val proxy = proxies.optJSONObject(name)
                val type = proxy?.optString("type")?.trim()?.lowercase() ?: providerNodes[name]?.second ?: continue
                if (type.isEmpty() || type in GROUP_PROXY_TYPES || type in RESERVED_PROXY_TYPES) continue
                candidates += name
            }
            candidates.toList()
        } catch (_: Exception) {
            null
        }
    }

    internal fun parseDelayResponse(json: String): Int? {
        return try {
            val delay = JSONObject(json).opt("delay") as? Number ?: return null
            delay.toInt().takeIf { it in 1..65_535 }
        } catch (_: Exception) {
            null
        }
    }

    internal fun selectLowestLatency(nodes: Collection<NodeLatency>): NodeLatency? =
        nodes.asSequence().filter { it.delayMs > 0 }.minByOrNull { it.delayMs }

    internal fun goMaxProcsForAvailableProcessors(availableProcessors: Int): Int =
        (availableProcessors - 1).coerceIn(1, 2)

    internal fun coreProcessCommand(
        corePath: String,
        arguments: List<String>,
        niceExecutable: String?,
        inheritedNice: Int = 0,
    ): List<String> = buildList {
        // nice adds to the caller's priority; a background launcher already inherits NI=10.
        if (!niceExecutable.isNullOrBlank() && inheritedNice < 10) {
            addAll(listOf(niceExecutable, "-n", (10 - inheritedNice).toString()))
        }
        add(corePath)
        addAll(arguments)
    }

    internal fun normalizeProbeUrl(value: String): String? {
        if (value.isBlank() || value.length > 4096 || value.any { it.isISOControl() || it.isWhitespace() }) return null
        return try {
            val uri = URI(value)
            if (!uri.scheme.equals("https", ignoreCase = true) || uri.host.isNullOrBlank()) return null
            if (uri.rawUserInfo != null) return null
            buildString {
                append("https://")
                append(uri.rawAuthority)
                append(uri.rawPath.ifBlank { "/" })
                uri.rawQuery?.let { append('?').append(it) }
            }.takeIf { runCatching { URI(it) }.isSuccess }
        } catch (_: Exception) {
            null
        }
    }

    private fun isReservedProxyName(name: String): Boolean = name.trim().lowercase() in RESERVED_PROXY_NAMES

    private fun encodePathSegment(value: String): String =
        URLEncoder.encode(value, StandardCharsets.UTF_8.name()).replace("+", "%20")

    private fun encodeQueryParameter(value: String): String =
        URLEncoder.encode(value, StandardCharsets.UTF_8.name())

    private fun isControllerListening(): Boolean = try {
        Socket().use { socket ->
            socket.connect(InetSocketAddress("127.0.0.1", CONTROLLER_PORT), 250)
        }
        true
    } catch (_: Exception) {
        false
    }

    internal fun prepareRuntimeConfig(yaml: String, secret: String): String? {
        if (!secret.matches(Regex("[0-9a-f]{64}"))) return null
        val migrated = migrateFootballGroupToSelector(yaml) ?: return null
        val lines = migrated.split('\n').toMutableList()
        setTopLevelSetting(lines, "external-controller", "127.0.0.1:$CONTROLLER_PORT")
        setTopLevelSetting(lines, "secret", "\"$secret\"")
        return lines.joinToString("\n")
    }

    private fun migrateFootballGroupToSelector(yaml: String): String? {
        val output = ArrayList<String>()
        var inGroups = false
        var inFootballGroup = false
        var groupFound = false
        var typeFound = false
        val groupName = Regex("^  - name:\\s*(?:\"([^\"]+)\"|'([^']+)'|([^#\\s]+))\\s*(?:#.*)?$")
        val typeLine = Regex("^    type:\\s*.*$")
        val removeSelectorUnsupported = Regex(
            "^    (?:url|interval|timeout|expected-status|tolerance|lazy|max-failed-times|empty-fallback|strategy):\\s*.*$",
        )

        for (rawLine in yaml.split('\n')) {
            val line = rawLine.removeSuffix("\r")
            if (line == "proxy-groups:") {
                inGroups = true
                inFootballGroup = false
                output += rawLine
                continue
            }
            if (inGroups && line.isNotBlank() && !line.startsWith(" ")) {
                inGroups = false
                inFootballGroup = false
            }
            if (inGroups && line.startsWith("  - ")) {
                val match = groupName.matchEntire(line)
                val name = match?.groupValues?.drop(1)?.firstOrNull { it.isNotEmpty() }
                inFootballGroup = name == PROXY_GROUP_NAME
                if (inFootballGroup) groupFound = true
                output += rawLine
                continue
            }
            if (!inFootballGroup) {
                output += rawLine
                continue
            }
            if (typeLine.matches(line)) {
                output += "    type: select"
                typeFound = true
                continue
            }
            if (removeSelectorUnsupported.matches(line)) continue
            output += rawLine
        }
        if (!groupFound || !typeFound) return null
        return output.joinToString("\n")
    }

    private fun setTopLevelSetting(lines: MutableList<String>, key: String, value: String) {
        val setting = Regex("^${Regex.escape(key)}:\\s*.*$")
        var replaced = false
        for (index in lines.indices) {
            val line = lines[index].removeSuffix("\r")
            if (!setting.matches(line)) continue
            if (!replaced) {
                lines[index] = "$key: $value"
                replaced = true
            } else {
                lines[index] = ""
            }
        }
        if (!replaced) lines.add(0, "$key: $value")
    }

    private fun getOrCreateControllerSecret(home: File): String? {
        val file = File(home, CONTROLLER_SECRET_NAME)
        readControllerSecret(file)?.let { return it }
        val random = ByteArray(32).also { SecureRandom().nextBytes(it) }
        val secret = random.joinToString("") { byte -> "%02x".format(byte.toInt() and 0xff) }
        return secret.takeIf { writeAtomically(file, it) }
    }

    private fun readControllerSecret(file: File): String? {
        return try {
            if (!file.isFile || file.length() > 128) return null
            file.readText(Charsets.UTF_8).trim().takeIf { it.matches(Regex("[0-9a-f]{64}")) }
        } catch (_: Exception) {
            null
        }
    }

    private fun ensureReadyLocked(context: Context): Boolean {
        val home = File(context.filesDir, "football-vpn").apply { mkdirs() }
        if (!ensureConfigFileLocked(context, home)) return false

        val controllerSecret = readControllerSecret(File(home, CONTROLLER_SECRET_NAME))
        if (isListening() && isControllerListening() && controllerSecret != null &&
            controllerRequest(controllerSecret, "GET", "/version") != null
        ) return true
        if (isListening()) {
            // A cached configuration from an older app may have only the HTTP proxy listener.
            // Restart a process owned by this instance so it picks up the new loopback controller.
            val running = process?.takeIf { it.isAlive } ?: return false
            stopProcessLocked()
            if (running.isAlive) return false
        }
        process?.takeIf { it.isAlive }?.let { running ->
            if (waitForListener(running)) return true
            running.destroyForcibly()
            process = null
            if (Thread.currentThread().isInterrupted) return false
        }

        // Android 14 blocks executing newly written files from app data. Gradle
        // packages one immutable executable per ABI under lib/ via jniLibs.
        val core = File(context.applicationInfo.nativeLibraryDir, "libmihomo.so")
        if (!core.isFile || !core.canExecute()) return false

        val config = File(home, CONFIG_NAME)
        File(home, "providers").mkdirs()
        val coreArguments = listOf("-d", home.absolutePath, "-f", config.absolutePath)
        val nice = File(NICE_EXECUTABLE).takeIf { it.isFile && it.canExecute() }?.absolutePath
        val inheritedNice = runCatching {
            android.os.Process.getThreadPriority(android.os.Process.myTid())
        }.getOrDefault(0)
        val maxProcs = goMaxProcsForAvailableProcessors(Runtime.getRuntime().availableProcessors())
        fun start(command: List<String>): Process? = try {
            ProcessBuilder(command)
                .directory(home)
                .redirectErrorStream(true)
                .apply { environment()["GOMAXPROCS"] = maxProcs.toString() }
                .start()
        } catch (_: Exception) {
            null
        }
        val child = start(coreProcessCommand(core.absolutePath, coreArguments, nice, inheritedNice))
            ?: if (nice != null) {
                Log.w(TAG, "core launch failed; retrying Mihomo without nice")
                start(coreProcessCommand(core.absolutePath, coreArguments, niceExecutable = null))
            } else {
                null
            }
            ?: return false
        backgroundThreadFactory("football-vpn-core-output").newThread {
            runCatching {
                child.inputStream.use { input ->
                    val buffer = ByteArray(4096)
                    while (input.read(buffer) >= 0) Unit
                }
            }
        }.start()
        process = child
        if (waitForListener(child)) return true
        child.destroyForcibly()
        if (process === child) process = null
        return false
    }

    private fun ensureConfigFileLocked(context: Context, home: File): Boolean {
        val configFile = File(home, CONFIG_NAME)
        val cached = readSubscriptionConfig(File(home, SUBSCRIPTIONS_NAME))
        val current = readValidLocalConfig(configFile)
        var base = current ?: readBundledConfig(context) ?: return false
        if (cached != null && extractSubscriptionUrls(base) != cached.subscriptions) {
            base = replaceSubscriptionUrls(base, cached.subscriptions) ?: return false
        }
        val secret = getOrCreateControllerSecret(home) ?: return false
        val runtimeConfig = prepareRuntimeConfig(base, secret) ?: return false
        if (current == runtimeConfig) return true
        return writeAtomically(configFile, runtimeConfig)
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

    private fun backgroundThreadFactory(namePrefix: String, daemon: Boolean = true): ThreadFactory {
        val sequence = AtomicInteger()
        return ThreadFactory { runnable ->
            Thread({
                runCatching {
                    android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_BACKGROUND)
                }
                runnable.run()
            }, "$namePrefix-${sequence.incrementAndGet()}").apply {
                isDaemon = daemon
            }
        }
    }

    private fun waitForListener(child: Process): Boolean {
        val deadline = SystemClock.elapsedRealtime() + START_TIMEOUT_MS
        while (SystemClock.elapsedRealtime() < deadline) {
            if (Thread.currentThread().isInterrupted) return false
            if (!child.isAlive) return false
            if (isListening() && isControllerListening()) return true
            SystemClock.sleep(100)
        }
        return isListening() && isControllerListening()
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

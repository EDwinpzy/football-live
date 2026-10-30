package com.qiubo.optimaltv.ui.player

import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.OtvLog
import com.qiubo.optimaltv.data.model.LiveSourceLine
import com.qiubo.optimaltv.data.model.LiveStreamSource
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.data.repo.LiveSourcePolicy
import com.qiubo.optimaltv.playback.EnginePlayState
import com.qiubo.optimaltv.playback.EngineRegistry
import com.qiubo.optimaltv.playback.MediaEngine
import com.qiubo.optimaltv.playback.PrepareRequest
import com.qiubo.optimaltv.playback.PlaybackHealthLog
import com.qiubo.optimaltv.playback.PlaybackHealthSnapshot
import com.qiubo.optimaltv.playback.PlaybackRecoveryPolicy
import com.qiubo.optimaltv.playback.RecoveryAction
import com.qiubo.optimaltv.data.analytics.PlaybackTelemetry
import kotlinx.coroutines.Job
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private fun upstreamFingerprint(relayUrl: String): String {
    val upstream = runCatching { android.net.Uri.parse(relayUrl).getQueryParameter("u") }.getOrNull()
        ?.takeIf { it.startsWith("https://") || it.startsWith("http://") } ?: return "unknown"
    return java.security.MessageDigest.getInstance("SHA-256").digest(upstream.toByteArray(Charsets.UTF_8))
        .joinToString("") { byte -> "%02x".format(byte.toInt() and 0xff) }
}

internal object PlaybackRecoveryCoordinationPolicy {
    fun canStartRecovery(
        startedThisSession: Boolean,
        awaitingPrepare: Boolean,
        sourceResolving: Boolean,
        recoveryInFlight: Boolean,
    ): Boolean = startedThisSession && !awaitingPrepare && !sourceResolving && !recoveryInFlight

    fun nextReadyStallTicks(
        ticks: Int,
        previousPositionMs: Long,
        positionMs: Long,
        previousRenderedFrames: Int,
        renderedFrames: Int,
    ): Int {
        if (previousPositionMs == -1L) return 0
        val clockAdvanced = positionMs != previousPositionMs
        val framesAdvanced = renderedFramesAdvanced(previousRenderedFrames, renderedFrames)
        return if (clockAdvanced || framesAdvanced) 0 else ticks + 1
    }

    fun renderedFramesAdvanced(previous: Int, current: Int): Boolean =
        previous >= 0 && current >= 0 && current > previous

    /** 首帧看门狗的宽限期（READY 之后还要等多久才判「无首帧」）。 */
    const val FIRST_FRAME_GRACE_MS = 15_000L

    /**
     * READY 之后是否已经等够宽限期、可以判定「解码器挂死无首帧」。
     *
     * 关键：计时起点必须是**首次 READY**，不是 prepare。上游慢时（实测 16.5s 才 READY）
     * 若从 prepare 起算，READY 一到预算就耗尽，只剩一个轮询窗口——而 VLC 从
     * READY（buffering 100%）到 vout 要 0.6–1.8s，于是刚起好的会话被误杀重来。
     * 起播前的长时间等待归 20s 起播看门狗（[armWatchdog]）管，不归这里。
     *
     * @param readyAtMs 首次观察到 READY 的时刻（elapsedRealtime）；<0 表示还没 READY 过。
     */
    fun firstFrameWatchdogExpired(
        readyAtMs: Long,
        nowMs: Long,
        firstFrameRendered: Boolean,
    ): Boolean = readyAtMs >= 0 && !firstFrameRendered && nowMs - readyAtMs >= FIRST_FRAME_GRACE_MS

    fun hasPlaybackProgress(positionAdvanced: Boolean, renderedFramesAdvanced: Boolean): Boolean =
        positionAdvanced || renderedFramesAdvanced
}

/**
 * 「更多」面板刷新判据：只接受信息量不减的一版，且当前集号在新列表里仍然有效。
 * 集数变少（源站抖动 / 部分线路解析失败）一律拒绝，免得把用户正在看的选集列表打回原形。
 */
data class PlayerUiState(
    val title: String = "球迹直播",
    val lines: List<LiveSourceLine> = emptyList(),
    val activeLine: Int = 0,
    val sourceResolving: Boolean = false,
    val engineId: String = "media3",
    val engineName: String = "",
    val playState: EnginePlayState = EnginePlayState.IDLE,
    val isPlaying: Boolean = false,
    val positionMs: Long = 0,
    val durationMs: Long = 0,
    val controlsVisible: Boolean = false,
    val isLive: Boolean = true,
    val srcPickerVisible: Boolean = false,
    val bufferedAheadMs: Long = 0,
    val engineEpoch: Int = 0,
    val fatalMsg: String? = null,
)
object PlayerStartupStatePolicy {
    fun initial(@Suppress("UNUSED_PARAMETER") matchId: String): PlayerUiState = PlayerUiState(
        title = "球迹直播",
        isLive = true,
        sourceResolving = true,
    )
}
/** 播放会话协调器：起播看门狗、恢复、换线、线路记忆与断点续播。 */
class PlayerViewModel(
    private val matchId: String,
    private val settings: SettingsStore,
    private val autoBoot: Boolean = true,
) : ViewModel() {
    companion object {
        const val TAG = "OTV"
        const val START_WATCHDOG_MS = 20_000L      // 直播起播超时（保持原行为）
        const val STALL_TICKS_LIMIT = 8            // 8s 位置无推进判定卡顿中断
                /** 长时体检采样节拍（2026-09-16 用户需求①）：10s 一次，冻结按连续窗口累计。 */
        const val HEALTH_TICK_MS = 10_000L
        /** 体检行节拍：每 5 分钟一行（按墙上时间算——跨会话重启不断档）。 */
        const val HEALTH_LOG_INTERVAL_MS = 300_000L
        /** 连续 3 个采样（~30s）位置在前进、渲染帧数不增 → 画面冻结（既有两道看门狗不响的形态）。 */
        const val HEALTH_FREEZE_TICKS = 3
        /** 冻结判定所需的最小位置推进量：过滤「真的卡在缓冲」（那种形态交给既有看门狗）。 */
        const val HEALTH_FREEZE_MIN_ADVANCE_MS = 3_000L
        /** 连续 6 个采样（60s）稳定出画才算「这条线真的活了」——见 onSessionStarted 说明。 */
        const val HEALTH_HEALTHY_TICKS = 6
    }

    private val _ui = MutableStateFlow(PlayerStartupStatePolicy.initial(matchId))
    val ui: StateFlow<PlayerUiState> = _ui.asStateFlow()

    lateinit var engine: MediaEngine
        private set

    fun hasEngine(): Boolean = this::engine.isInitialized

    private var startedThisSession = false
    private var awaitingPrepare = false
    private var sessionGen = 0
    private var handledEndedSessionGen = -1
    private var stallRecoveries = 0
    private var recoveryAttempt = 0
    private var stallRecoveryJob: Job? = null
    private var stallRecoveryGeneration = 0
    private var triedLines = mutableSetOf<Int>()
    private var watchdogJob: Job? = null
    private var stallJob: Job? = null
    private var healthJob: Job? = null
    private var backendAlive = false
    private val windowProbeClient by lazy {
        okhttp3.OkHttpClient.Builder()
            .connectTimeout(2, java.util.concurrent.TimeUnit.SECONDS)
            .readTimeout(4, java.util.concurrent.TimeUnit.SECONDS)
            .build()
    }
    private var lastHealthLogAt = 0L
    private var telemetry: PlaybackTelemetry.Session = PlaybackTelemetry.newSession()
    private var renewJob: Job? = null
    private var liveEndedRecoveries = 0
    private var liveAutoRetryJob: Job? = null
    private var liveAutoRetries = 0
    private val liveSrcs = mutableListOf<String>()
    private val liveReferers = mutableMapOf<String, String>()
    private var liveMatchId: String? = null
    private var livePlayingUrl: String? = null
    private var sessionUrl: String = ""
    private val renewKick = kotlinx.coroutines.channels.Channel<Unit>(kotlinx.coroutines.channels.Channel.CONFLATED)
    private var sessionViaRelay = false
    private var engineEpochCounter = 0
    private var engineStateJob: Job? = null
    private var firstFrameJob: Job? = null
    private var lastAutoSwitchAt = -1L
    private var sameLineRetries = 0
    private var softwareFallbackTried = false
    private var vlcFallbackTried = false
    private var audioCodecChecked = false
    private val relayBroken = mutableSetOf<String>()
    private var userPaused = false
    private var systemResumeNeeded = false
    private var hostPausedAtMs = 0L
    private fun scheduleLiveAutoRetry() {
        if (!_ui.value.isLive || liveAutoRetries >= 5) return
        liveAutoRetryJob?.cancel()
        liveAutoRetries++
        liveAutoRetryJob = viewModelScope.launch {
            delay(30_000)
            if (_ui.value.fatalMsg != null && _ui.value.isLive) {
                triedLines.clear()
                _ui.value = _ui.value.copy(fatalMsg = null)
                boot(fresh = true)
            }
        }
    }
    /** 直播：与 lines 平行的后端动态信号源 ID，续签时只重解析当前源 */
    private suspend fun refreshLiveSource(source: String, fallbackName: String = ""): LiveStreamSource? {
    val currentMatchId = liveMatchId ?: return null
    return Graph.live.refreshLiveStreamSource(
        matchId = currentMatchId,
        source = source,
        fresh = true,
        fallbackName = fallbackName,
        fallbackReferer = liveReferers[source].orEmpty(),
    )?.also { liveReferers[it.source] = it.referer }
}

private suspend fun refreshLivePlaybackUrl(source: String, fallbackName: String = ""): String? =
    refreshLiveSource(source, fallbackName)?.playbackUrl
private fun installEngine(newEngine: MediaEngine) {
        engine = newEngine
        engineEpochCounter++
        _ui.value = _ui.value.copy(
            engineId = newEngine.engineId,
            engineName = newEngine.displayName,
            engineEpoch = engineEpochCounter,
        )
    }

    /**
     * 进错误兜底态前先停播（2026-09-21）：错误卡是「无法播放」语义，旧版只把画面遮住、
     * 不停引擎，实测「卡片已弹出、背景音照常响」。重试会重新 prepare，不需要保留播放态。
     */
    private fun stopForFatal() {
        awaitingPrepare = false
        runCatching { if (hasEngine()) engine.pause() }
    }

        init {
        if (autoBoot) viewModelScope.launch { boot() }
    }
    private suspend fun bootLive(fresh: Boolean) {
        val matchId = this.matchId
        if (liveMatchId != matchId) liveEndedRecoveries = 0
        liveMatchId = matchId
        // 标题异步补：matches() 冷抓可能 20s+，绝不能挡在起播链路上（LiveScreen 进入过则为缓存命中）
        viewModelScope.launch {
            val m = runCatching { Graph.live.matches().find { it.matchId == matchId } }.getOrNull()
            if (m != null) _ui.value = _ui.value.copy(title = "${m.home} 对阵 ${m.away}")
        }
        // 后端在本次请求内解析、探测所有候选线；按它返回的可播放线路及优先顺序展示。
        liveSrcs.clear()
        livePlayingUrl = null
        relayBroken.clear()
        liveReferers.clear()
        _ui.value = _ui.value.copy(
            lines = emptyList(), activeLine = 0, isLive = true,
            sourceResolving = true, fatalMsg = null, controlsVisible = false,
        )
        val sources = try {
            Graph.live.liveStreamSources(matchId, fresh = fresh)
        } catch (cancelled: kotlinx.coroutines.CancellationException) {
            throw cancelled
        } catch (failure: Exception) {
            OtvLog.w("bootLive 动态线路解析失败 match=$matchId: ${failure.message}")
            emptyList()
        }
        currentCoroutineContext().ensureActive()
        _ui.value = _ui.value.copy(sourceResolving = false)
        val remembered = runCatching { Graph.settings.liveLastSrc() }.getOrNull().orEmpty()
        val availableIds = sources.map { it.source }
        val preferred = LiveSourcePolicy.preferred(remembered, availableIds)
        if (preferred.isNotBlank() && remembered.isNotBlank() && remembered != preferred) {
            runCatching { Graph.settings.rememberLiveLastSrc(preferred) }
        }
        OtvLog.i("bootLive match=$matchId 可播放信号源=${sources.size} 个")
        if (sources.isEmpty()) {
            OtvLog.w("bootLive 无可用信号 → fatalMsg")
            stopForFatal()
            _ui.value = _ui.value.copy(fatalMsg = "直播信号暂不可用", title = "球迹直播", isLive = true)
            scheduleLiveAutoRetry()
            return
        }
        liveSrcs.addAll(sources.map { it.source })
        liveReferers.putAll(sources.associate { it.source to it.referer })
        val lines = sources.map { LiveSourceLine(it.name, it.playbackUrl) }
        // 重试路径重建：先释放旧引擎（否则每次重试泄漏一个 ExoPlayer 实例）
        if (this::engine.isInitialized) runCatching { engine.release() }
        // 调试注入的引擎强制（am start --es otvEngine vlc，测试软解路径用；一次性消费）
        val engineOverride = com.qiubo.optimaltv.DebugNavBus.pendingEngine?.also {
            com.qiubo.optimaltv.DebugNavBus.pendingEngine = null
        }
        installEngine(EngineRegistry.create(engineOverride ?: settings.current().engineId, Graph.appContext))
        _ui.value = _ui.value.copy(
            title = "球迹直播", lines = lines, isLive = true,
            engineId = engine.engineId, engineName = engine.displayName,
        )
        collectEngineState()
        startLiveRenewal()
        // 记忆线仍存在时继续使用；失效时采用后端返回的首条可用线路。
        val selIdx = liveSrcs.indexOf(preferred).takeIf { it >= 0 } ?: 0
        if (selIdx > 0) OtvLog.i("bootLive：直选上次使用的可用线路 $preferred（第 ${selIdx + 1} 条）")
        startSession(selIdx, reuseResolvedUrl = true)
    }

    /** Refresh the selected live URL periodically and when a stall recovery requests it. */
    private fun startLiveRenewal() {
        renewJob?.cancel()
        renewJob = viewModelScope.launch {
            while (isActive) {
                // 90s 一轮；期间被 kick（直播卡顿）则立刻进入本轮
                kotlinx.coroutines.withTimeoutOrNull(90_000) { renewKick.receive() }
                if (liveMatchId == null) break
                val idx = _ui.value.activeLine
                val src = liveSrcs.getOrNull(idx) ?: continue
                val s = _ui.value
                if (s.fatalMsg != null) continue
                if (s.playState != EnginePlayState.READY && s.playState != EnginePlayState.BUFFERING) continue
                val playingUrl = livePlayingUrl
                if (playingUrl.isNullOrBlank()) continue
                if (!s.isPlaying && s.playState == EnginePlayState.READY) continue

                // Bind this network request to the selected source, session, and engine instance.
                // A slow response from the previous line must never replace a newer selection.
                val expectedEngine = engine
                val expectedEngineEpoch = engineEpochCounter
                val requestGeneration = sessionGen
                val freshUrl = try {
                    refreshLivePlaybackUrl(src)
                } catch (cancelled: kotlinx.coroutines.CancellationException) {
                    throw cancelled
                } catch (failure: Exception) {
                    OtvLog.w("live renew: source refresh failed: ${failure.message}")
                    null
                }
                if (!isCurrentPlaybackRequest(
                        requestGeneration, idx, expectedEngine, expectedEngineEpoch,
                    )) continue
                if (freshUrl.isNullOrBlank()) {
                    OtvLog.w("live renew：src=$src 解析失败（继续播当前流，下轮再试）")
                    continue
                }
                if (freshUrl == playingUrl) continue   // 签名未变化，无需动作
                // HTTP 200 on an old manifest does not prove its signed segments remain usable.
                // Prefer the new URL as soon as signing changes, before the old window expires.
                if (!isCurrentPlaybackRequest(
                        requestGeneration, idx, expectedEngine, expectedEngineEpoch,
                    )) continue
                OtvLog.i("live renew: refreshed URL changed; hot-switch line=$idx source=$src")
                writeBackLineUrl(idx, freshUrl)
                expectedEngine.swapSource(freshUrl)
                OtvLog.i("liveSourceRenewal upstreamFingerprint=${upstreamFingerprint(freshUrl)}")
                livePlayingUrl = freshUrl
            }
        }
    }

    /** 线路表 URL 回写（续签热切换/暂停静默续签后，手动换回该线时用新地址） */
    private fun writeBackLineUrl(idx: Int, freshUrl: String) {
        _ui.value.lines.getOrNull(idx)?.let { old ->
            _ui.value = _ui.value.copy(
                lines = _ui.value.lines.toMutableList().also { it[idx] = old.copy(url = freshUrl) },
            )
        }
    }

    /** Reject results that crossed a newer selection or engine replacement while suspended. */
    private fun isCurrentPlaybackRequest(
        generation: Int,
        lineIndex: Int,
        expectedEngine: MediaEngine,
        expectedEngineEpoch: Int,
    ): Boolean = hasEngine() && engine === expectedEngine &&
        PlaybackSessionPolicy.isCurrent(
            requestGeneration = generation,
            currentGeneration = sessionGen,
            requestLine = lineIndex,
            activeLine = _ui.value.activeLine,
            requestEngineEpoch = expectedEngineEpoch,
            currentEngineEpoch = engineEpochCounter,
        )

    /** 引擎状态镜像收集；重试重建引擎时先取消旧收集协程 */
    private fun collectEngineState() {
    engineStateJob?.cancel()
    val observedEngine = engine
    engineStateJob = viewModelScope.launch {
        observedEngine.uiState.collect { state ->
            if (engine !== observedEngine) return@collect
            val hidden = !_ui.value.controlsVisible
            _ui.value = _ui.value.copy(
                playState = state.state,
                isPlaying = state.isPlaying,
                positionMs = if (hidden) state.positionMs / 1_000 * 1_000 else state.positionMs,
                durationMs = state.durationMs,
                bufferedAheadMs = if (hidden) state.bufferedAheadMs / 1_000 * 1_000 else state.bufferedAheadMs,
            )
            when {
                state.state == EnginePlayState.ENDED && startedThisSession -> handlePlaybackEnded()
                state.state == EnginePlayState.READY && awaitingPrepare && !startedThisSession ->
                    onSessionStarted(state.positionMs)
                state.state == EnginePlayState.ERROR && (startedThisSession || awaitingPrepare) -> {
                    val decodeFailure = state.errorMsg?.contains("DECODING_FAILED") == true
                    if (decodeFailure && engine.engineId == "media3" && !softwareFallbackTried) {
                        softwareFallbackTried = true
                        engine.setSoftwareDecoderPreferred(true)
                        viewModelScope.launch { startSession(_ui.value.activeLine, reuseResolvedUrl = true) }
                    } else if (decodeFailure && engine.engineId == "media3" && !vlcFallbackTried) {
                        vlcFallbackTried = true
                        switchEngine("vlc")
                    } else {
                        onLineFail("引擎错误: ${state.errorMsg}")
                    }
                }
            }
        }
    }
}

private fun handlePlaybackEnded() {
    if (handledEndedSessionGen == sessionGen) return
    handledEndedSessionGen = sessionGen
    when (PlaybackSessionPolicy.actionAfterLiveEnd(liveEndedRecoveries)) {
        PlaybackSessionPolicy.LiveEndAction.RESTART_CURRENT -> {
            liveEndedRecoveries++
            OtvLog.w("live HLS window ended; fresh-restart current line once")
            viewModelScope.launch { recreateEngineForRecovery(_ui.value.activeLine) }
        }
        PlaybackSessionPolicy.LiveEndAction.STOP -> {
            val state = _ui.value
            stopForFatal()
            _ui.value = state.copy(fatalMsg = "直播信号已结束或暂时中断，请切换线路或重试")
        }
    }
}

private suspend fun boot(fresh: Boolean = false) {
    try {
        bootLive(fresh)
    } catch (cancelled: kotlinx.coroutines.CancellationException) {
        throw cancelled
    } catch (failure: Exception) {
        currentCoroutineContext().ensureActive()
        OtvLog.w("球迹直播初始化失败: ${failure.message}")
        stopForFatal()
        _ui.value = _ui.value.copy(sourceResolving = false, fatalMsg = "球迹直播暂不可用，请重试")
        scheduleLiveAutoRetry()
    }
}
private suspend fun startSession(
    lineIdx: Int,
    reuseResolvedUrl: Boolean = false,
) {
    val generation = ++sessionGen
    val sessionEngine = engine
    val sessionEngineEpoch = engineEpochCounter
    awaitingPrepare = false
    startedThisSession = false
    stallRecoveries = 0
    triedLines += lineIdx
    _ui.value = _ui.value.copy(activeLine = lineIdx, fatalMsg = null, sourceResolving = !reuseResolvedUrl)
    val line = _ui.value.lines.getOrNull(lineIdx) ?: run {
        _ui.value = _ui.value.copy(sourceResolving = false)
        onLineFail("线路缺失（index=$lineIdx）")
        return
    }
    val source = liveSrcs.getOrNull(lineIdx)
    val freshUrl = if (!reuseResolvedUrl && source != null) {
        try {
            refreshLivePlaybackUrl(source, line.label)
        } catch (cancelled: kotlinx.coroutines.CancellationException) {
            throw cancelled
        } catch (failure: Exception) {
            OtvLog.w("直播线路刷新失败 ${line.label}: ${failure.message}")
            null
        }
    } else null
    currentCoroutineContext().ensureActive()
    if (!isCurrentPlaybackRequest(generation, lineIdx, sessionEngine, sessionEngineEpoch)) return
    val playUrl = freshUrl ?: line.url
    if (freshUrl != null) {
        _ui.value = _ui.value.copy(
            lines = _ui.value.lines.toMutableList().also { it[lineIdx] = line.copy(url = freshUrl) },
        )
    }
    _ui.value = _ui.value.copy(sourceResolving = false)
    if (playUrl.isBlank()) {
        onLineFail("线路解析失败（${line.label}）")
        return
    }
    sessionViaRelay = playUrl.contains("/api/relay")
    livePlayingUrl = playUrl
    sessionUrl = playUrl
    if (playUrl.contains("m3u8", ignoreCase = true)) {
        val (windowMs, segmentMs) = probeLiveWindowMs(playUrl)
        currentCoroutineContext().ensureActive()
        if (!isCurrentPlaybackRequest(generation, lineIdx, sessionEngine, sessionEngineEpoch)) return
        sessionEngine.setLiveWindowHintMs(windowMs, segmentMs)
    }
    val sourceFingerprint = source?.takeIf { it.isNotBlank() }?.let {
        java.security.MessageDigest.getInstance("SHA-256").digest(it.toByteArray(Charsets.UTF_8))
            .joinToString("") { byte -> "%02x".format(byte.toInt() and 0xff) }
    } ?: "unknown"
    OtvLog.i("startSession line=$lineIdx engine=${sessionEngine.engineId} sourceFingerprint=$sourceFingerprint upstreamFingerprint=${upstreamFingerprint(playUrl)}")
    telemetry.mark(
        PlaybackTelemetry.Stage.PLAYER_PREPARE,
        source = playUrl,
        detail = "engine=${sessionEngine.engineId} line=$lineIdx",
    )
    if (!isCurrentPlaybackRequest(generation, lineIdx, sessionEngine, sessionEngineEpoch)) return
    awaitingPrepare = true
    try {
        sessionEngine.prepare(PrepareRequest(playUrl))
    } catch (cancelled: kotlinx.coroutines.CancellationException) {
        awaitingPrepare = false
        throw cancelled
    } catch (failure: Exception) {
        awaitingPrepare = false
        startedThisSession = false
        onLineFail("引擎起播失败: ${failure.message ?: "未知错误"}")
        return
    }
    armWatchdog()
    armFirstFrameWatchdog()
    startStallMonitor()
    startHealthMonitor()
}

private fun armFirstFrameWatchdog() {
    firstFrameJob?.cancel()
    firstFrameJob = viewModelScope.launch {
        var readyAt = -1L
        while (isActive) {
            val state = _ui.value
            if (state.fatalMsg != null || state.playState == EnginePlayState.ERROR || state.playState == EnginePlayState.ENDED) return@launch
            val firstFrame = if (hasEngine()) engine.uiState.value.firstFrameRendered else true
            if (firstFrame) return@launch
            val now = android.os.SystemClock.elapsedRealtime()
            if (state.playState != EnginePlayState.READY) {
                readyAt = -1L
                delay(500)
                continue
            }
            if (readyAt < 0) readyAt = now
            if (!PlaybackRecoveryCoordinationPolicy.firstFrameWatchdogExpired(
                    readyAt, now, firstFrame,
                )) {
                delay(500)
                continue
            }
            OtvLog.w("直播 READY 后 ${PlaybackRecoveryCoordinationPolicy.FIRST_FRAME_GRACE_MS / 1000}s 无首帧")
            if (hasEngine() && engine.engineId == "media3" && !vlcFallbackTried) {
                vlcFallbackTried = true
                switchEngine("vlc")
            } else {
                onLineFail("画面无输出（首帧超时）")
            }
            return@launch
        }
    }
}

private fun nextUntriedSource(): Int? {
    val state = _ui.value
    val now = android.os.SystemClock.elapsedRealtime()
    return state.lines.indices.firstOrNull { index ->
        index != state.activeLine && index !in triedLines &&
            LongPlaybackHealthPolicy.canSwitchNow(now, lastAutoSwitchAt, index, state.activeLine)
    }
}

private fun markAutoSwitch(targetIndex: Int) {
    if (targetIndex != _ui.value.activeLine) lastAutoSwitchAt = android.os.SystemClock.elapsedRealtime()
}
private fun armWatchdog() {
    watchdogJob?.cancel()
    watchdogJob = viewModelScope.launch {
        delay(START_WATCHDOG_MS)
        if (!startedThisSession) onLineFail("起播超时（${START_WATCHDOG_MS / 1000}s 无画面）")
    }
}
private fun startStallMonitor() {
        stallJob?.cancel()
        stallJob = viewModelScope.launch {
            var lastPos = -1L
            var lastRendered = -1
            var lastBuf = -1L
            var ticks = 0
            // 缓冲/停滞的连续累计时长（毫秒）：交给 LongPlaybackHealthPolicy 判档。
            var bufferingMs = 0L
            // 已在当前缓冲周期里触发过的最高档位，避免同一周期反复重复动作。
            var lastBufferAction = LongPlaybackHealthPolicy.Action.HEALTHY
            while (isActive) {
                delay(1_000)
                val s = _ui.value
                val renderedFrames = if (hasEngine()) {
                    runCatching { engine.decoderStats()?.rendered ?: -1 }.getOrDefault(-1)
                } else {
                    -1
                }
                if (s.playState == EnginePlayState.BUFFERING && !userPaused) {
                    bufferingMs += 1_000L
                    // 2026-09-28：buffer 还在涨 = 正常补片，不是卡死。旧版只看
                    // 「BUFFERING 满 8s」就把正常抖动判成卡死并换线（用户侧观感是
                    // 「看十几分钟莫名换台」）。现在交给策略结合 buffer 增长判定。
                    val bufferGrowing = s.bufferedAheadMs > lastBuf
                    if (bufferGrowing) {
                        if (lastBufferAction != LongPlaybackHealthPolicy.Action.HEALTHY) {
                            telemetry.mark(
                                PlaybackTelemetry.Stage.BUFFER_END,
                                detail = "recovered buf=${s.bufferedAheadMs}ms",
                            )
                        }
                        bufferingMs = 0
                        lastBufferAction = LongPlaybackHealthPolicy.Action.HEALTHY
                    } else {
                        val action = LongPlaybackHealthPolicy.action(
                            bufferingMs = bufferingMs,
                            bufferGrowing = false,
                            positionMoving = false,
                            hasBackup = s.lines.indices.any { it != s.activeLine },
                        )
                        telemetry.mark(PlaybackTelemetry.Stage.BUFFER_START, detail = "buf=${s.bufferedAheadMs}ms")
                        // 只在档位「变严重」时动作一次，别在同一档里反复触发。
                        if (action != LongPlaybackHealthPolicy.Action.HEALTHY &&
                            action != lastBufferAction
                        ) {
                            lastBufferAction = action
                            when (action) {
                                LongPlaybackHealthPolicy.Action.PREP_BACKUP ->
                                    OtvLog.i("缓冲 ${bufferingMs}ms 无增长 → 预备备用线（不打断）")
                                LongPlaybackHealthPolicy.Action.SWITCH -> {
                                    OtvLog.w("缓冲 ${bufferingMs}ms 无增长 → 切备用线")
                                    telemetry.mark(PlaybackTelemetry.Stage.AUTO_SWITCH, detail = "reason=buffering")
                                    recoverPlaybackStall("BUFFERING ${bufferingMs / 1000}s 无进展")
                                }
                                LongPlaybackHealthPolicy.Action.DEGRADE_ENGINE -> {
                                    OtvLog.w("缓冲 ${bufferingMs}ms 无增长 → 引擎降级")
                                    if (hasEngine() && engine.engineId == "media3" && !vlcFallbackTried) {
                                        vlcFallbackTried = true
                                        telemetry.mark(
                                            PlaybackTelemetry.Stage.ENGINE_FALLBACK,
                                            errorClass = PlaybackTelemetry.ErrorClass.DECODE,
                                            detail = "buffering ${bufferingMs}ms → vlc",
                                        )
                                        switchEngine("vlc")
                                    } else {
                                        recoverPlaybackStall("BUFFERING ${bufferingMs / 1000}s 无进展")
                                    }
                                }
                                LongPlaybackHealthPolicy.Action.GIVE_UP -> {
                                    OtvLog.e("缓冲 ${bufferingMs}ms 无增长 → 放弃当前线")
                                    recoverPlaybackStall("BUFFERING ${bufferingMs / 1000}s 无进展")
                                }
                                LongPlaybackHealthPolicy.Action.HEALTHY -> Unit
                            }
                        }
                    }
                    ticks = 0
                    lastPos = s.positionMs
                    lastRendered = renderedFrames
                } else if (s.playState == EnginePlayState.READY && s.isPlaying) {
                    if (bufferingMs > 0) {
                        telemetry.mark(PlaybackTelemetry.Stage.BUFFER_END, detail = "buf=${s.bufferedAheadMs}ms")
                    }
                    bufferingMs = 0
                    lastBufferAction = LongPlaybackHealthPolicy.Action.HEALTHY
                    ticks = PlaybackRecoveryCoordinationPolicy.nextReadyStallTicks(
                        ticks = ticks,
                        previousPositionMs = lastPos,
                        positionMs = s.positionMs,
                        previousRenderedFrames = lastRendered,
                        renderedFrames = renderedFrames,
                    )
                    lastPos = s.positionMs
                    lastRendered = renderedFrames
                    if (ticks >= STALL_TICKS_LIMIT) {
                        ticks = 0
                        recoverPlaybackStall("READY 但位置停滞 ${STALL_TICKS_LIMIT}s")
                    }
                } else {
                    ticks = 0
                    lastPos = -1
                    lastRendered = -1
                    bufferingMs = 0
                    lastBufferAction = LongPlaybackHealthPolicy.Action.HEALTHY
                }
                lastBuf = s.bufferedAheadMs
            }
        }
    }

    /**
     * 长时体检 + 冻结取证（2026-09-16 用户需求①）。
     *
     * 每 10s 采样一次，跑在 viewModelScope 默认的 Main 调度器上——ExoPlayer 的
     * getVideoDecoderCounters() 会校验应用线程，必须主线程取数。
     *
     * 两条产出：
     * - 每 5 分钟一行 [HEALTH]（引擎/位置/缓冲/渲染帧数/丢帧/中继/后端/解密池/内存/网络）：
     *   卡死前那几分钟的走向一眼可见，重启 app 后仍能从 files/logs/otv.log 回看；
     * - 「位置在前进、渲染帧数不增」连续 30s = 画面冻住（既有看门狗只认 BUFFERING 超时与
     *   位置不前进，对这种形态不响）→ 先打 [STALL-DUMP] 留现场，再交给既有恢复阶梯
     *   （刷新→换源→重建引擎→重置运行时，有限次；不含整机自重启）。
     */
    private fun startHealthMonitor() {
        healthJob?.cancel()
        healthJob = viewModelScope.launch {
            var lastRendered = -1
            var lastPos = -1L
            var frozenTicks = 0
            var healthyTicks = 0
            while (isActive) {
                delay(HEALTH_TICK_MS)
                val s = _ui.value
                if (!hasEngine() || s.fatalMsg != null) {
                    frozenTicks = 0; lastRendered = -1; lastPos = -1L
                    continue
                }
                val es = engine.uiState.value
                val dec = runCatching { engine.decoderStats() }.getOrNull()
                backendAlive = probeBackendAlive()
                val playing = es.state == EnginePlayState.READY && es.isPlaying && !userPaused
                val renderedFramesAdvanced = PlaybackRecoveryCoordinationPolicy.renderedFramesAdvanced(
                    previous = lastRendered,
                    current = dec?.rendered ?: -1,
                )
                if (playing && dec != null && lastRendered >= 0) {
                    val advanced = es.positionMs - lastPos
                    if (dec.rendered > lastRendered) frozenTicks = 0
                    else if (advanced >= HEALTH_FREEZE_MIN_ADVANCE_MS) frozenTicks++ else frozenTicks = 0
                } else {
                    frozenTicks = 0
                }
                val positionMoving = lastPos >= 0 && es.positionMs > lastPos
                val positionAdvanced = lastPos >= 0 && es.positionMs - lastPos >= HEALTH_FREEZE_MIN_ADVANCE_MS
                val playbackProgressed = PlaybackRecoveryCoordinationPolicy.hasPlaybackProgress(
                    positionAdvanced = positionAdvanced,
                    renderedFramesAdvanced = renderedFramesAdvanced,
                )
                lastRendered = dec?.rendered ?: -1
                lastPos = es.positionMs
                // 僵尸源修复（见 onSessionStarted）：稳定出画满 60s 才恢复同线重试配额
                if (playing && playbackProgressed) {
                    healthyTicks++
                    if (healthyTicks >= HEALTH_HEALTHY_TICKS) {
                        healthyTicks = 0
                        recoveryAttempt = 0
                        if (sameLineRetries != 0) {
                            sameLineRetries = 0
                            OtvLog.i("本线稳定播放 60s → 同线重试配额恢复")
                        }
                    }
                } else {
                    healthyTicks = 0
                }
                if (frozenTicks >= HEALTH_FREEZE_TICKS) {
                    // 先取用本次累计值再清零：frozenMs 要用于冻结判定，不能被重置覆盖。
                    val frozenMs = frozenTicks.toLong() * HEALTH_TICK_MS
                    frozenTicks = 0
                    val secs = frozenMs / 1000
                    // 冻结 = 位置在前进但渲染帧数不增：换源对同编码内容无效，直接降级引擎。
                    // 冻结时重试同一条信号源对解码/渲染管线挂死毫无意义；按
                    // LongPlaybackHealthPolicy 的 DECODE 判定降级引擎。
                    val isFrozen = LongPlaybackHealthPolicy.isFrozen(
                        positionMoving = positionMoving,
                        renderedNotIncreasingForMs = frozenMs,
                    )
                    OtvLog.e("画面冻结：位置前进但渲染帧数 ${secs}s 不增 → 交给既有恢复阶梯")
                    PlaybackHealthLog.freeze(healthSnapshot("画面冻结 ${secs}s（时钟前进、无新帧）"))
                    if (isFrozen) {
                        telemetry.mark(
                            PlaybackTelemetry.Stage.ENGINE_FALLBACK,
                            errorClass = PlaybackTelemetry.ErrorClass.DECODE,
                            detail = "frozen ${frozenMs}ms position moving, no new frame",
                        )
                        val action = LongPlaybackHealthPolicy.action(
                            bufferingMs = 0L,
                            bufferGrowing = true, // buffer 满也不能代表能出画面
                            positionMoving = true,
                            hasBackup = _ui.value.lines.indices.any { it != _ui.value.activeLine },
                            errorClass = PlaybackTelemetry.ErrorClass.DECODE,
                        )
                        if (action == LongPlaybackHealthPolicy.Action.DEGRADE_ENGINE &&
                            hasEngine() && engine.engineId == "media3" && !vlcFallbackTried
                        ) {
                            vlcFallbackTried = true
                            OtvLog.w("画面冻结 ${secs}s → 切 libVLC（解码类冻结换线无效）")
                            switchEngine("vlc")
                        } else {
                            // 没有引擎可降（或已在 vlc）→ 交回通用阶梯换线/重建。
                            recoverPlaybackStall("画面冻结 ${secs}s（时钟前进、无新帧）")
                        }
                    } else {
                        recoverPlaybackStall("画面冻结 ${secs}s（时钟前进、无新帧）")
                    }
                } else if (android.os.SystemClock.elapsedRealtime() - lastHealthLogAt >= HEALTH_LOG_INTERVAL_MS) {
                    lastHealthLogAt = android.os.SystemClock.elapsedRealtime()
                    PlaybackHealthLog.tick(healthSnapshot("5 分钟体检"))
                }
            }
        }
    }

    /** 体检快照取数（主线程；全部 runCatching，取不到就记 -1）。 */
    private fun healthSnapshot(note: String): PlaybackHealthSnapshot {
        val s = _ui.value
        val es = if (hasEngine()) engine.uiState.value else com.qiubo.optimaltv.playback.EngineUiState()
        val line = s.lines.getOrNull(s.activeLine)
        return PlaybackHealthSnapshot(
            kind = "live",
            content = s.title,
            lineIndex = s.activeLine + 1,
            lineLabel = line?.label.orEmpty().ifBlank { "-" },
            lineCount = s.lines.size,
            url = livePlayingUrl.orEmpty(),
            engineId = if (hasEngine()) engine.engineId else "-",
            engineName = if (hasEngine()) engine.displayName else "-",
            engineEpoch = s.engineEpoch,
            state = es.state.name,
            isPlaying = es.isPlaying,
            positionMs = es.positionMs,
            durationMs = es.durationMs,
            bufferedAheadMs = es.bufferedAheadMs,
            firstFrame = es.firstFrameRendered,
            decoder = if (hasEngine()) runCatching { engine.decoderStats() }.getOrNull() else null,
            viaRelay = sessionViaRelay,
            relayBroken = relayBroken.size,
            backendStarted = backendAlive,
            decryptSlots = runCatching { com.qiubo.optimaltv.playback.StreamDecryptServer.readySlots() }.getOrDefault(-1),
            recoveryStep = recoveryAttempt,
            stallRecoveries = stallRecoveries,
            note = note,
        )
    }

    /**
     * 内嵌后端探活（长时体检用）：后端跑在 `:backend` 独立进程，主进程读不到它的
     * started 标志，因此按「能不能连上本地端口」判断——这也正是播放链路真实依赖的形态。
     * 走 IO 调度器 + 300ms 超时，不阻塞主线程，也不会因为后端线程僵住而卡住体检本身。
     */
    private suspend fun probeBackendAlive(): Boolean = withContext(Dispatchers.IO) {
        runCatching {
            java.net.Socket().use {
                it.connect(java.net.InetSocketAddress("127.0.0.1", com.qiubo.optimaltv.EmbeddedBackend.port()), 300)
            }
        }.isSuccess
    }

    /**
     * 直播窗口探测（2026-09-16 长跑卡顿根治）：把媒体清单里所有 `#EXTINF` 时长相加，
     * 得到「可播放窗口」长度（毫秒）。起播前调用一次（走本地中继，约 0.2–0.6s）。
     *
     * 拿不到（主清单 #EXT-X-STREAM-INF / 请求失败 / 超时）返回 0 = 未知，引擎沿用旧档，
     * 行为与修复前一致——探测失败不会让直播更糟。
     */
    private suspend fun probeLiveWindowMs(url: String): Pair<Long, Long> = withContext(Dispatchers.IO) {
        runCatching {
            val req = okhttp3.Request.Builder().url(url).build()
            windowProbeClient.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return@use 0L to 0L
                readWindowMs(resp.body?.string().orEmpty())
            }
        }.getOrDefault(0L to 0L)
    }

    /** 纯函数：清单文本 → 窗口毫秒（无 EXTINF 视为未知 0）。 */
    private fun readWindowMs(body: String): Pair<Long, Long> {
        var sum = 0.0
        var longest = 0.0
        var n = 0
        for (line in body.lineSequence()) {
            val t = line.trim()
            if (!t.startsWith("#EXTINF", ignoreCase = true)) continue
            val v = t.substringAfter(":", "").substringBefore(",").trim().toDoubleOrNull() ?: continue
            sum += v
            longest = maxOf(longest, v)
            n++
        }
        return if (n > 0) (sum * 1000).toLong() to (longest * 1000).toLong() else 0L to 0L
    }

    /** Bounded live recovery ladder shared with the mobile client. */
    private fun recoverPlaybackStall(reason: String) {
    val state = _ui.value
    if (!hasEngine() || state.fatalMsg != null) return
    if (!PlaybackRecoveryCoordinationPolicy.canStartRecovery(
            startedThisSession = startedThisSession,
            awaitingPrepare = awaitingPrepare,
            sourceResolving = state.sourceResolving,
            recoveryInFlight = stallRecoveryJob?.isActive == true,
        )) return
    val requestSessionGeneration = sessionGen
    val requestLine = state.activeLine
    val requestEngine = engine
    val requestEngineEpoch = engineEpochCounter
    val requestToken = ++stallRecoveryGeneration
    val job = viewModelScope.launch(start = CoroutineStart.LAZY) {
        try {
            if (!isCurrentStallRecovery(requestToken, requestSessionGeneration, requestLine, requestEngine, requestEngineEpoch)) return@launch
            val attempt = recoveryAttempt++
            // Exhaust untried live lines before rebuilding a player on a failing CDN.
            val action = PlaybackRecoveryPolicy.action(attempt, hasAlternate = nextUntriedSource() != null)
            val current = _ui.value.activeLine
            runCatching { PlaybackHealthLog.stall(healthSnapshot(reason)) }
            OtvLog.w("live recovery step=$attempt action=$action reason=$reason")
            when (action) {
                RecoveryAction.REFRESH_CURRENT -> startRecoverySession(requestToken, current)
                RecoveryAction.SWITCH_SOURCE -> {
                    val next = nextUntriedSource()
                    if (next == null) recreateEngineForRecovery(current, requestToken)
                    else {
                        markAutoSwitch(next)
                        startRecoverySession(requestToken, next)
                    }
                }
                RecoveryAction.RECREATE_ENGINE -> recreateEngineForRecovery(current, requestToken)
                RecoveryAction.RESET_RUNTIME -> {
                    com.qiubo.optimaltv.playback.StreamDecryptServer.reset()
                    com.qiubo.optimaltv.BackendService.reboot(Graph.appContext, viewModelScope)
                    delay(1_800)
                    startRecoverySession(requestToken, current)
                }
                RecoveryAction.GIVE_UP -> {
                    stopForFatal()
                    _ui.value = _ui.value.copy(fatalMsg = "播放持续卡顿，已完成自动恢复")
                    scheduleLiveAutoRetry()
                }
            }
        } finally {
            if (requestToken == stallRecoveryGeneration) stallRecoveryJob = null
        }
    }
    stallRecoveryJob = job
    job.start()
}
private fun isCurrentStallRecovery(
        token: Int,
        generation: Int,
        lineIndex: Int,
        expectedEngine: MediaEngine,
        expectedEngineEpoch: Int,
    ): Boolean = token == stallRecoveryGeneration &&
        isCurrentPlaybackRequest(generation, lineIndex, expectedEngine, expectedEngineEpoch)

    private suspend fun startRecoverySession(
        token: Int,
        line: Int,
        reuseResolvedUrl: Boolean = false,
    ) {
        if (token != stallRecoveryGeneration) return
        startSession(line, reuseResolvedUrl = reuseResolvedUrl)
        val generation = sessionGen
        // Keep single-flight until READY or the existing 20s watchdog/onLineFail resolves prepare.
        while (token == stallRecoveryGeneration && generation == sessionGen &&
            awaitingPrepare && !startedThisSession && _ui.value.fatalMsg == null
        ) {
            delay(100)
        }
    }

    private suspend fun recreateEngineForRecovery(line: Int, recoveryToken: Int? = null) {
    val id = engine.engineId
    runCatching { engine.release() }
    installEngine(EngineRegistry.create(id, Graph.appContext))
    _ui.value = _ui.value.copy(engineId = engine.engineId, engineName = engine.displayName, engineEpoch = engineEpochCounter)
    collectEngineState()
    if (recoveryToken != null) startRecoverySession(recoveryToken, line)
    else startSession(line)
}
private fun cancelStallRecovery() {
        stallRecoveryGeneration++
        stallRecoveryJob?.cancel()
        stallRecoveryJob = null
    }

    /** 手动切换直播线路时先重建播放器，避免旧请求覆盖当前线路。 */
    private fun recreateEngineForManualSourceChange() {
    if (!hasEngine()) return
    val id = engine.engineId
    runCatching { engine.release() }
    installEngine(EngineRegistry.create(id, Graph.appContext))
    collectEngineState()
}
private suspend fun onSessionStarted(posMs: Long) {
    startedThisSession = true
    awaitingPrepare = false
    watchdogJob?.cancel()
    firstFrameJob?.cancel()
    liveAutoRetries = 0
    liveAutoRetryJob?.cancel()
    Log.i(TAG, "READY line=${_ui.value.activeLine} @$posMs ms")
    OtvLog.i("READY line=${_ui.value.activeLine} @$posMs ms（出画面）")
    runCatching {
        val started = _ui.value.lines.getOrNull(_ui.value.activeLine)
        telemetry.mark(PlaybackTelemetry.Stage.FIRST_FRAME, source = started?.url.orEmpty(), detail = "line=${_ui.value.activeLine}")
        OtvLog.i(telemetry.summary(started?.url.orEmpty()).format())
    }
    runCatching { PlaybackHealthLog.start(healthSnapshot("起播")) }
    armLiveAudioCodecCheck()
}

private fun armLiveAudioCodecCheck() {
    if (audioCodecChecked || !hasEngine() || engine.engineId != "media3") return
    audioCodecChecked = true
    viewModelScope.launch {
        delay(1_500)
        if (!hasEngine() || engine.engineId != "media3") return@launch
        val codecs = runCatching { engine.audioCodecs() }.getOrNull()
        if (codecs != null && Regex("ac-3|eac-3|ec-3|dts", RegexOption.IGNORE_CASE).containsMatchIn(codecs)) {
            OtvLog.w("直播音频编解码不可解（$codecs）→ 切 libVLC 重起")
            switchEngine("vlc")
        }
    }
}
private fun switchEngine(targetId: String) {
    if (!hasEngine()) return
    runCatching { engine.release() }
    installEngine(EngineRegistry.create(targetId, Graph.appContext))
    _ui.value = _ui.value.copy(engineId = engine.engineId, engineName = engine.displayName, engineEpoch = engineEpochCounter)
    collectEngineState()
    viewModelScope.launch { startSession(_ui.value.activeLine, reuseResolvedUrl = true) }
}

private fun onLineFail(reason: String) {
    if (_ui.value.fatalMsg != null) return
    awaitingPrepare = false
    if (reason.contains("DECODING_FAILED")) {
        OtvLog.w("直播解码失败 → 等待 30s 自动重试: $reason")
        stopForFatal()
        _ui.value = _ui.value.copy(fatalMsg = reason)
        scheduleLiveAutoRetry()
        return
    }
    val current = _ui.value.activeLine
    val hasAlternate = _ui.value.lines.indices.any { it != current && it !in triedLines }
    when (PlaybackRecoveryPolicy.liveLineFailureAction(sameLineRetries, hasAlternate)) {
        RecoveryAction.REFRESH_CURRENT -> {
            sameLineRetries++
            OtvLog.w("line fail($current): $reason → 同线 fresh 重解析")
            viewModelScope.launch { startSession(current) }
        }
        RecoveryAction.SWITCH_SOURCE -> {
            sameLineRetries = 0
            val next = _ui.value.lines.indices.firstOrNull { it != current && it !in triedLines }
            if (next == null) {
                stopForFatal()
                _ui.value = _ui.value.copy(fatalMsg = reason)
                scheduleLiveAutoRetry()
            } else {
                OtvLog.w("line fail($current): $reason → 切换备用源 $next")
                viewModelScope.launch { startSession(next) }
            }
        }
        else -> {
            sameLineRetries = 0
            stopForFatal()
            _ui.value = _ui.value.copy(fatalMsg = reason)
            scheduleLiveAutoRetry()
        }
    }
}
// ---------- 用户操作 ----------

    fun retry() {
    cancelStallRecovery()
    recoveryAttempt = 0
    triedLines.clear()
    sameLineRetries = 0
    lastAutoSwitchAt = -1L
    liveAutoRetries = 0
    liveEndedRecoveries = 0
    softwareFallbackTried = false
    vlcFallbackTried = false
    _ui.value = _ui.value.copy(fatalMsg = null)
    val lines = _ui.value.lines
    if (lines.isEmpty()) viewModelScope.launch { boot(fresh = true) }
    else viewModelScope.launch { startSession(_ui.value.activeLine.coerceIn(lines.indices)) }
}

fun switchLine(idx: Int) {
    val state = _ui.value
    if (idx !in state.lines.indices || idx == state.activeLine) return
    cancelStallRecovery()
    recoveryAttempt = 0
    renewJob?.cancel()
    sessionGen++
    liveEndedRecoveries = 0
    awaitingPrepare = false
    startedThisSession = false
    recreateEngineForManualSourceChange()
    _ui.value = _ui.value.copy(activeLine = idx, fatalMsg = null, sourceResolving = true, playState = EnginePlayState.BUFFERING)
    triedLines.clear()
    sameLineRetries = 0
    lastAutoSwitchAt = -1L
    liveSrcs.getOrNull(idx)?.let { source ->
        viewModelScope.launch { runCatching { settings.rememberLiveLastSrc(source) } }
    }
    touch()
    val generation = sessionGen
    val selectedEngine = engine
    val selectedEpoch = engineEpochCounter
    viewModelScope.launch {
        if (!isCurrentPlaybackRequest(generation, idx, selectedEngine, selectedEpoch)) return@launch
        startSession(idx)
        startLiveRenewal()
    }
}
// ---- 直播信号源选择条（直播播放器唯一控件，需求 足球#14）----

    /** OK/菜单键呼出/收起信号源选择条 */
    fun toggleSrcPicker() {
        _ui.value = _ui.value.copy(srcPickerVisible = !_ui.value.srcPickerVisible)
        touch()
    }

    fun hideSrcPicker() {
        if (_ui.value.srcPickerVisible) {
            _ui.value = _ui.value.copy(srcPickerVisible = false)
        }
    }

    // ---- v1.21 DLNA 投屏：2026-09-11 随需求⑭追加从 TV 版整体剥离（移动版保留）----

    private fun touch() = Unit

fun onHostPause() {
    if (hostPausedAtMs == 0L) hostPausedAtMs = android.os.SystemClock.elapsedRealtime()
    if (_ui.value.isPlaying && hasEngine()) {
        systemResumeNeeded = true
        engine.pause()
    }
}

fun onHostResume() {
    val decision = com.qiubo.optimaltv.lifecycle.AppLifecyclePolicy.onForeground(
        backgroundedAtMs = hostPausedAtMs,
        nowMs = android.os.SystemClock.elapsedRealtime(),
        isLive = true,
    )
    hostPausedAtMs = 0L
    if (systemResumeNeeded && !userPaused && hasEngine()) engine.play()
    systemResumeNeeded = false
    when (decision) {
        com.qiubo.optimaltv.lifecycle.ForegroundDecision.REFRESH_AND_RENEW_LIVE -> recoverPlaybackStall("后台超过 30 秒，刷新直播地址")
        com.qiubo.optimaltv.lifecycle.ForegroundDecision.REFRESH -> renewKick.trySend(Unit)
        com.qiubo.optimaltv.lifecycle.ForegroundDecision.NONE -> Unit
    }
}
override fun onCleared() {
    runCatching { if (hasEngine()) engine.release() }
    renewJob?.cancel()
    liveAutoRetryJob?.cancel()
    firstFrameJob?.cancel()
    stallJob?.cancel()
    healthJob?.cancel()
    watchdogJob?.cancel()
    super.onCleared()
}
}

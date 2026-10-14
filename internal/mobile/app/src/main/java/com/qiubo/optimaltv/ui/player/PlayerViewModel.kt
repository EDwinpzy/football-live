package com.qiubo.optimaltv.ui.player

import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.OtvLog
import com.qiubo.optimaltv.data.cast.DlnaControl
import com.qiubo.optimaltv.data.cast.DlnaDevice
import com.qiubo.optimaltv.data.cast.DlnaDiscovery
import com.qiubo.optimaltv.data.db.HistoryEntity
import com.qiubo.optimaltv.data.db.ProgressEntity
import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.data.model.LiveStreamSource
import com.qiubo.optimaltv.data.prefs.AspectMode
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.data.repo.SelectedSourceUnavailableException
import com.qiubo.optimaltv.data.repo.VodRepository
import com.qiubo.optimaltv.playback.EnginePlayState
import com.qiubo.optimaltv.playback.EngineRegistry
import com.qiubo.optimaltv.playback.MediaEngine
import com.qiubo.optimaltv.playback.PrepareRequest
import com.qiubo.optimaltv.playback.PlaybackKind
import com.qiubo.optimaltv.playback.PlaybackHealthLog
import com.qiubo.optimaltv.playback.PlaybackHealthSnapshot
import com.qiubo.optimaltv.playback.PlaybackRecoveryPolicy
import com.qiubo.optimaltv.playback.RecoveryAction
import com.qiubo.optimaltv.data.analytics.PlaybackTelemetry
import kotlinx.coroutines.Job
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

enum class Panel { NONE, MORE }
enum class PlayerSourceStatus { LOADING, READY, EMPTY, ERROR }

/** Small, pure coordination rules shared by the live stall watchdog and health sampler. */
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

    fun hasPlaybackProgress(positionAdvanced: Boolean, renderedFramesAdvanced: Boolean): Boolean =
        positionAdvanced || renderedFramesAdvanced
}

/** A selected VOD line is also an explicit same-line retry; live source picking stays unchanged. */
internal object ManualLineSelectionPolicy {
    fun shouldStart(lineCount: Int, selected: Int, active: Int, isLive: Boolean): Boolean =
        selected in 0 until lineCount && (selected != active || !isLive)

    fun resolutionFailureAction(failure: Throwable): ResolutionFailureAction =
        if (failure is SelectedSourceUnavailableException) {
            ResolutionFailureAction.KEEP_SELECTED_LINE
        } else {
            ResolutionFailureAction.USE_EXISTING_FAILOVER
        }
}

internal enum class ResolutionFailureAction { KEEP_SELECTED_LINE, USE_EXISTING_FAILOVER }

/**
 * 「更多」面板刷新判据：只接受信息量不减的一版，且当前集号在新列表里仍然有效。
 * 集数变少（源站抖动 / 部分线路解析失败）一律拒绝，免得把用户正在看的选集列表打回原形。
 */
object EpisodeRefreshPolicy {
    fun accepts(
        currentNames: List<String>,
        incomingNames: List<String>,
        currentLines: Int,
        incomingLines: Int,
        keepIndex: Int,
        sameEpisodeSource: Boolean = true,
    ): Boolean {
        if (keepIndex !in 0 until incomingNames.size) return false
        if (sameEpisodeSource && incomingNames.size < currentNames.size) return false
        if (incomingNames.size > currentNames.size) return true
        if (incomingLines > currentLines) return true
        // 同集数但集名/线路内容变了：仍然替换，否则「更新了但不显示」。
        return incomingNames != currentNames
    }

    internal fun isCurrentRequest(
        request: EpisodeRefreshTicket,
        latestRequestId: Long,
        vodId: String,
        detailRef: String,
        playbackGeneration: Int,
        episodeIndex: Int,
        activeLine: Int,
        sourceIdentity: String?,
    ): Boolean = request.requestId == latestRequestId &&
        request.vodId == vodId && request.detailRef == detailRef &&
        request.playbackGeneration == playbackGeneration && request.episodeIndex == episodeIndex &&
        request.activeLine == activeLine && request.sourceIdentity == sourceIdentity
}

internal data class EpisodeRefreshTicket(
    val requestId: Long,
    val vodId: String,
    val detailRef: String,
    val playbackGeneration: Int,
    val episodeIndex: Int,
    val activeLine: Int,
    val sourceIdentity: String?,
)

data class PlayerUiState(
    val title: String = "",
    val epIndex: Int = 0,
    val epCount: Int = 1,
    /** 选集名（更多面板选集网格展示用；懒解析详情后回填） */
    val epNames: List<String> = emptyList(),
    /** 影片信息（更多面板展示：meta「2026 / 中国 / 剧情」+ 简介 + 海报） */
    val meta: String = "",
    val desc: String = "",
    val posterUrl: String = "",
    /** 需求⑨修订：电影=true——更多面板隐藏选集区；电视剧/动漫/短剧保留选集 */
    val isMovie: Boolean = false,
    val lines: List<LineInfo> = emptyList(),
    val activeLine: Int = 0,
    /** Current football source lookup is in progress; independent of engine buffering. */
    val sourceResolving: Boolean = false,
    /** 片源列表/当前线路状态，供错误态的侧栏展示查找、空结果、加载与失败重试。 */
    val sourceStatus: PlayerSourceStatus = PlayerSourceStatus.LOADING,
    val engineId: String = "media3",
    val engineName: String = "",
    // 引擎状态镜像（驱动 UI）
    val playState: EnginePlayState = EnginePlayState.IDLE,
    val isPlaying: Boolean = false,
    val positionMs: Long = 0,
    val durationMs: Long = 0,
    val speed: Float = 1f,
    /** 长按倍速镜像（2026-09-21 移动端手势）：非 null = 画面左右侧被按住，值 = 临时档位；
     *  松手回落到 [speed]，浮层提示与倍速胶囊都以它为准。 */
    val speedBoost: Float? = null,
    val aspectOrdinal: Int = 0,
    // UI 层状态
    val controlsVisible: Boolean = false,
    val panel: Panel = Panel.NONE,
    val showExitDialog: Boolean = false,
    /** 全部线路尝试失败后的兜底错误（ErrorWidget，方案 §5 三层防线） */
    val fatalMsg: String? = null,
    /** 直播模式：隐藏进度条/时间/数字跳转，不落进度库 */
    val isLive: Boolean = false,
    /** 直播信号源选择条可见（直播播放器唯一控件，需求 足球#14：无任何菜单） */
    val srcPickerVisible: Boolean = false,
    /** 正向缓冲量镜像（直播续签缓冲保护） */
    val bufferedAheadMs: Long = 0,
    /** v1.16 引擎实例代数：每次安装新引擎实例（首建/VLC 回退）+1。电视页视频层以代数为
     *  key——换台/换线热切换（复用同一引擎实例）不重建渲染面，旧画面停帧过渡到新首帧，
     *  消除换台黑屏闪烁；仅引擎实例真正更换（libVLC 回退）才重建。 */
    val engineEpoch: Int = 0,
    // ---- v1.21 DLNA 投屏 ----
    /** 设备扫描中（浮层转圈） */
    val castScanning: Boolean = false,
    /** 已发现的渲染器列表 */
    val castDevices: List<com.qiubo.optimaltv.data.cast.DlnaDevice> = emptyList(),
    /** 投屏中的设备（null = 本地播放；非 null 时本地引擎暂停、控制改道 SOAP） */
    val castDevice: com.qiubo.optimaltv.data.cast.DlnaDevice? = null,
    /** 投屏播放进度镜像（渲染器 GetPositionInfo 2s 轮询） */
    val castPositionMs: Long = 0L,
    /** 投屏进度总长（渲染器上报；直播常为 0） */
    val castDurationMs: Long = 0L,
    /** 投屏失败提示（一次性消费：UI toast 后调 clearCastError） */
    val castError: String? = null,
)

/** Route identity is known before any source request, including a slow football lookup. */
object PlayerStartupStatePolicy {
    fun initial(vodId: String, epIndex: Int): PlayerUiState {
        val football = vodId.startsWith("live:")
        return PlayerUiState(epIndex = epIndex, isLive = football, sourceResolving = football)
    }
}

/** 播放会话协调器：起播看门狗、恢复、换线、线路记忆与断点续播。 */
class PlayerViewModel(
    // v1.15：val → var——电视页内嵌播放（TvScreen）在同一 VM 会话内换台时经 switchIptv 更新；
    // PlayerScreen（足球/影视/调试直达）行为不变（构造后不再改）。
    private var vodId: String,
    private val epStart: Int,
    private val repo: VodRepository,
    private val db: com.qiubo.optimaltv.data.db.AppDb,
    private val settings: SettingsStore,
    /** v1.15：电视页内嵌场景传 false——构造不自动起播，由页面在频道列表就绪后
     *  显式 switchIptv(上次频道) 起播。默认 true（PlayerScreen 原行为）。 */
    private val autoBoot: Boolean = true,
    /** v1.19 跨线路续播：详情页扫全部线路算出的最近观看位置随路由带入；
     *  >0 时优先生效（本线路 DB 查询兜底「最近观看」直达路径） */
    private val resumeStartMs: Long = 0L,
) : ViewModel() {

    companion object {
        const val TAG = "OTV"
        const val START_WATCHDOG_MS = 20_000L      // 方案 §3.2-1 jellyplay BUFFERING_TIMEOUT_MS
        const val STALL_TICKS_LIMIT = 8            // 8s 位置无推进判定卡顿中断
        const val PROGRESS_INTERVAL_MS = 5_000L
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
        val SPEED_STEPS = listOf(0.5f, 0.75f, 1.0f, 1.25f, 1.5f, 2.0f, 3.0f)
    }

    private val _ui = MutableStateFlow(PlayerStartupStatePolicy.initial(vodId, epStart))
    val ui: StateFlow<PlayerUiState> = _ui.asStateFlow()

    lateinit var engine: MediaEngine
        private set

    fun hasEngine(): Boolean = this::engine.isInitialized

    private var startedThisSession = false
    /** Ignore READY from the old paused item while a manual request is still resolving. */
    private var awaitingPrepare = false
    /** 会话代数：并发的 startSession 只允许最新一代操作引擎（防旧源倒灌） */
    private var sessionGen = 0
    /** 手动动作开始时立即使旧的解析/起播结果过期，并取消旧协程。 */
    private var manualSessionJob: Job? = null
    private var manualSessionRequest = 0
    /** 重试找不到片源时替换正在执行的目录/详情加载。 */
    private var bootJob: Job? = null
    private var bootGeneration = 0

    /* ---------- 2026-09-28 取源阶梯 / 快速起播 / 埋点 ---------- */

    /** 本次取源实际生效的层级（L0_last_good / L1_cloud_resolver / L2_local_detail）。 */
    private var resolveTier: String = "none"

    /** 线路已由 L0（本地 last_good）命中 → 点播放可直接 prepare，不必等任何网络往返。 */
    private var resolvePrefillReady: Boolean = false

    /** 一次「点播放 → 出画面」的计时上下文；出首帧时汇总 total_click_to_frame。 */
    private val telemetry = PlaybackTelemetry.Session(vodId)

    /**
     * 长播健康冷却窗（换源/换引擎后防抖）。
     *
     * 状态机化是为了修一个具体的自激循环：旧版这里是裸字段，换线时装弹、
     * 紧接着新线 READY 时 [onSessionStarted] 无条件清零，于是「刚换的线正在起播、
     * 缓冲属正常」这段最该被保护的窗口立刻失效 → 健康采样又判卡顿 → 再换线。
     * 现在起播不再清它，只有稳定播放满 60s（[onStablePlayback]）或会话结束才清。
     */
    private val healthCooldown = LongPlaybackHealthPolicy.Cooldown()

    /**
     * 会话级恢复预算（僵尸源封顶）。阶梯本身在 PlaybackRecoveryPolicy 里收敛，
     * 但 `recoveryAttempt` 每次起播成功都清零 —— 「能出画面、几秒后必断」的线
     * 可以无限循环。预算只被真正稳定播放（[onStablePlayback]）或换片/重试重置。
     */
    private val recoveryBudget = LongPlaybackHealthPolicy.RecoveryBudget()

    /** 连续 BUFFERING 起始时刻（0 = 未在缓冲中）。 */
    private var bufferingSince: Long = 0L

    /** 上一次健康采样时的缓冲量（判断缓冲是否还在增长）。 */
    private var lastBufferedAheadMs: Long = 0L
    /** 防止同一引擎会话重复收到 ENDED 时重复切集。 */
    private var handledEndedSessionGen = -1
    private var stallRecoveries = 0
    /** Unified recovery ladder step; reset only after the new session reaches READY. */
    private var recoveryAttempt = 0
    /** One automatic stall-recovery action may resolve/prepare at a time. */
    private var stallRecoveryJob: Job? = null
    private var stallRecoveryGeneration = 0
    private var triedLines = mutableSetOf<Int>()
    private var watchdogJob: Job? = null
    private var stallJob: Job? = null
    /** 长时体检/冻结取证任务（2026-09-16 需求①）：见 PlaybackHealthLog 头注释 */
    private var healthJob: Job? = null
    /** 内嵌后端探活最近结果（体检 10s 刷新一次；起播快照沿用缓存值） */
    private var backendAlive = false
    /** 直播窗口探测用短超时客户端（本地中继；超时即返回未知，不干预播放） */
    private val windowProbeClient by lazy {
        okhttp3.OkHttpClient.Builder()
            .connectTimeout(2, java.util.concurrent.TimeUnit.SECONDS)
            .readTimeout(4, java.util.concurrent.TimeUnit.SECONDS)
            .build()
    }
    /** 上一次 5 分钟体检的墙上时间（跨会话重启保持节拍，长跑日志不会因换源而断档） */
    private var lastHealthLogAt = 0L
    private var progressJob: Job? = null
    private var episodeRefreshJob: Job? = null
    /** 直播静默续签循环（90s 重新解析当前源 → 签名变化即热切换） */
    private var renewJob: Job? = null
    /** One fresh same-line restart is allowed for an unexpectedly-ended live window. */
    private var liveEndedRecoveries = 0
    /** 直播错误态 30s 自动重试（直播稳定性方案修复 2 配套：未开赛/上游抖动场景
     *  「直播看不了」时不再要求用户手动按重试；连续 5 次仍失败则交还手动） */
    private var liveAutoRetryJob: Job? = null
    private var liveAutoRetries = 0
    /** 直播错误态自动重试：30s 后 fresh 重走全链路（最多 5 次；手动重试/成功起播重置计数） */
    private fun scheduleLiveAutoRetry() {
        if (!_ui.value.isLive) return
        liveAutoRetryJob?.cancel()
        if (liveAutoRetries >= 5) {
            OtvLog.w("直播自动重试已达上限（5 次），等待手动重试")
            return
        }
        liveAutoRetries++
        OtvLog.i("直播 30s 后自动重试（${liveAutoRetries}/5，fresh 重解析）")
        liveAutoRetryJob = viewModelScope.launch {
            delay(30_000)
            if (_ui.value.fatalMsg != null && _ui.value.isLive) {
                triedLines.clear()
                // v1.16 IPTV：30s 自动重试从主线路整队重启（不卡死在耗尽的备用线尾）
                if (vodId.startsWith("iptv:") && iptvPrimary != null) {
                    iptvRestartFromPrimary()
                    return@launch
                }
                _ui.value = _ui.value.copy(fatalMsg = null)
                boot(fresh = true)
            }
        }
    }

    /** Live source IDs and referers returned for this match; IDs stay aligned with [PlayerUiState.lines]. */
    private var liveSrcs: List<String> = emptyList()
    private val liveReferers = mutableMapOf<String, String>()
    private var liveMatchId: String? = null
    /** 引擎实际在播的直播地址（v1.14 续签对比基准；线路表只作展示/手动换线入口） */
    private var livePlayingUrl: String? = null

    /** Refresh exactly one previously probed dynamic source and keep its referer for later renewals. */
    private suspend fun refreshLiveSource(source: String, fallbackName: String = ""): LiveStreamSource? {
        val matchId = liveMatchId ?: return null
        val fallbackReferer = liveReferers[source].orEmpty()
        return Graph.live.refreshLiveStreamSource(
            matchId = matchId,
            source = source,
            fresh = true,
            fallbackName = fallbackName,
            fallbackReferer = fallbackReferer,
        )?.also { liveReferers[it.source] = it.referer }
    }

    private suspend fun refreshLivePlaybackUrl(source: String, fallbackName: String = ""): String? =
        refreshLiveSource(source, fallbackName)?.playbackUrl

    /** 当前会话的最终播放 URL（v1.21 投屏取投地址用；startSession 赋值） */
    private var sessionUrl: String = ""

    /** 投屏进度轮询协程（GetPositionInfo 2s → state 镜像） */
    private var castPollJob: Job? = null

    /** 投屏暂停判定辅助：Pause 发出时记录的位置（位置未动再按=恢复播放；-1=播放中） */
    private var castPausedAt = -1L
    /** 续签即时触发（v1.14：直播卡顿/恢复播放时立刻做健康检查，不等 90s tick） */
    private val renewKick = kotlinx.coroutines.channels.Channel<Unit>(kotlinx.coroutines.channels.Channel.CONFLATED)
    /** 本会话当前线路实际是否经中继（v1.14 点播阶梯：中继失败→同线直连重试） */
    private var sessionViaRelay = false
    /** 已判定中继不可用的线路 playRef 集合（v1.14）：会话内直接走直连，避免反复撞墙 */
    private val relayBroken = mutableSetOf<String>()
    /** 点播卡顿阶梯第②级（fresh 同线重解析）本段已用（防循环；正常前进 >60s 后重置） */
    private var didStallReResolve = false
    private var lastRecoveryPosMs = 0L
    /** v1.16 引擎实例代数（PlayerUiState.engineEpoch 的来源计数） */
    private var engineEpochCounter = 0

    /**
     * 引擎实例装配（2026-09-21「播放中突然黑屏只剩声音」修复）。
     *
     * 任何「新建并替换 engine 实例」的路径都必须走这里：实例代数 +1 写回 UI，播放页
     * 渲染层按 [PlayerUiState.engineEpoch] 换 Compose key 重建 AndroidView。
     * 旧版只在三段回退/卡顿阶梯里 +1，普通重试路径（boot/bootLive/bootReplay/
     * bootHhkanVid）重建实例却不 +1 → key 不变，视图树继续挂着**已 release** 的旧
     * PlayerView，新引擎的渲染面从未挂进窗口 ⇒ 整屏全黑、声音照常。
     */
    private fun installEngine(newEngine: MediaEngine) {
        engine = newEngine
        engineEpochCounter++
        _ui.value = _ui.value.copy(
            engineId = newEngine.engineId,
            engineName = newEngine.displayName,
            engineEpoch = engineEpochCounter,
        )
    }

    private fun isCurrentPlaybackRequest(
        requestGeneration: Int,
        lineIndex: Int,
        expectedEngine: MediaEngine,
        expectedEngineEpoch: Int,
    ): Boolean = hasEngine() && engine === expectedEngine &&
        PlaybackSessionPolicy.isCurrent(
            requestGeneration = requestGeneration,
            currentGeneration = sessionGen,
            requestLine = lineIndex,
            activeLine = _ui.value.activeLine,
            requestEngineEpoch = expectedEngineEpoch,
            currentEngineEpoch = engineEpochCounter,
        )

    /**
     * 进错误兜底态前先停播（2026-09-21）：错误卡是「无法播放」语义，旧版只把画面遮住、
     * 不停引擎，实测「卡片已弹出、背景音照常响」。重试会重新 prepare，不需要保留播放态。
     */
    private fun stopForFatal() {
        runCatching { if (hasEngine()) engine.pause() }
        if (!_ui.value.isLive) {
            _ui.value = _ui.value.copy(sourceStatus = PlayerSourceStatus.ERROR)
        }
    }

    /** A fresh refresh invalidated the selected reference; stop here and let the user retry/select. */
    private fun stopForSelectedSourceUnavailable(lineIdx: Int) {
        awaitingPrepare = false
        stopForFatal()
        _ui.value = _ui.value.copy(
            activeLine = lineIdx,
            fatalMsg = "所选线路或集数已失效，请重试或重新查找片源",
            controlsVisible = false,
            playState = EnginePlayState.IDLE,
        )
    }

    /** v1.16 IPTV 多源备份队列：当前频道的备用线路（switchIptv 传入，主线路失败时逐条热切换） */
    private val iptvBackups = ArrayDeque<String>()
    /** v1.16 当前频道的主线路与备用清单（30s 自动重试/手动重试从主线路整队重启） */
    private var iptvPrimary: Triple<String, String, List<String>>? = null
    private var digitBuffer = ""
    private var digitJob: Job? = null
    private var userPaused = false
    private var systemResumeNeeded = false
    private var hostPausedAtMs = 0L
    private var currentItem: com.qiubo.optimaltv.data.model.VodItem? = null
    private var episodeNamesSourceIdentity: String? = null
    private var episodeRefreshRequestId = 0L

    /** 足球回放/集锦（replay:/hhkanplay:）不进观看历史/进度库——与直播同规则，
     *  否则比赛录像以「埃弗顿 对阵 曼联 第1集」混进影视最近观看（用户反馈）。 */
    private val isFootballVod = vodId.startsWith("replay:") || vodId.startsWith("hhkanplay:")

    init {
        if (autoBoot) launchBoot()
    }

    private fun launchBoot(fresh: Boolean = false) {
        bootJob?.cancel()
        bootJob = viewModelScope.launch { boot(fresh) }
    }

    /** Start a latest-wins user request; cancelled network results are also rejected by sessionGen. */
    private fun launchManualSession(line: Int, positionMs: Long, recreateEngine: Boolean) {
        val request = beginManualPlaybackRequest()
        if (_ui.value.isLive) {
            renewJob?.cancel()
            liveEndedRecoveries = 0
        }
        if (!_ui.value.isLive) {
            recoveryBudget.reset()
            healthCooldown.reset()
            didStallReResolve = false
            recoveryAttempt = 0
        }
        manualSessionJob = viewModelScope.launch {
            if (request != manualSessionRequest) return@launch
            startSession(
                lineIdx = line,
                startPos = positionMs,
                freshResolve = !_ui.value.isLive,
                recreateEngine = recreateEngine,
            )
            if (_ui.value.isLive && liveMatchId != null && request == manualSessionRequest) {
                startLiveRenewal()
            }
        }
    }

    /** Invalidate every pending selection and recovery before a new user choice starts resolving. */
    private fun beginManualPlaybackRequest(): Int {
        cancelStallRecovery()
        manualSessionJob?.cancel()
        invalidateEpisodeRefresh()
        watchdogJob?.cancel()
        stallJob?.cancel()
        healthJob?.cancel()
        firstFrameJob?.cancel()
        awaitingPrepare = false
        startedThisSession = false
        manualSessionRequest++
        sessionGen++ // invalidate blocking resolvers synchronously, before the new coroutine runs
        return manualSessionRequest
    }

    private fun isCurrentManualPlaybackRequest(
        request: Int,
        generation: Int,
        lineIndex: Int,
        expectedEngine: MediaEngine,
        expectedEngineEpoch: Int,
    ): Boolean = request == manualSessionRequest && isCurrentPlaybackRequest(
        generation, lineIndex, expectedEngine, expectedEngineEpoch,
    )

    /**
     * v1.15 电视页内嵌换台（TvScreen 侧边栏选台 / 画面态 UP-DOWN 换台 / 失败自动跳台）：
     * 同一 VM 会话内切换 IPTV 频道——内部仍走 bootIptv 全链路（UrlGuard 字符串级+DNS
     * 解析级双重复验、直播看门狗、卡顿检测、错误态 30s 自动重试、DECODING_FAILED
     * 三段回退），不绕过任何安全校验。
     * v1.16：新增 [backups]（同频道备用线路）——主线路失败时 onLineFail 逐条热切换，
     * 全耗尽才进「错误态 30s 自动重试 + 页面跳台」旧链路。
     * 换台 = 新会话：换线/重试/回退配额与自动重试计数全部重置（等价新建 VM）。
     */
    fun switchIptv(name: String, url: String, backups: List<String> = emptyList()) {
        cancelStallRecovery()
        vodId = "iptv:$name|$url"
        iptvPrimary = Triple(name, url, backups)
        iptvBackups.clear()
        iptvBackups.addAll(backups)
        triedLines.clear()
        sameLineRetries = 0
        softwareFallbackTried = false
        vlcFallbackTried = false
        audioCodecChecked = false
        liveAutoRetries = 0
        liveEndedRecoveries = 0
        // 换台 = 新会话（同上：换线/重试/回退配额全部重置）
        recoveryBudget.reset()
        healthCooldown.reset()
        liveAutoRetryJob?.cancel()
        renewJob?.cancel()          // IPTV 本就不启用续签，防御性取消
        _ui.value = _ui.value.copy(
            title = name, isLive = true, fatalMsg = null,
            controlsVisible = false, panel = Panel.NONE, srcPickerVisible = false,
        )
        OtvLog.i("iptv switchIptv: $name（主线路+备用 ${backups.size} 条）")
        viewModelScope.launch { bootIptv() }
    }

    /** 回放分支（vodId = "replay:{urlencoded home|away}"）：原版 playRewatch 链路 */    private suspend fun bootReplay() {
        val raw = java.net.URLDecoder.decode(vodId.removePrefix("replay:"), "UTF-8")
        val parts = raw.split("|", limit = 2)
        val home = parts.getOrElse(0) { "" }; val away = parts.getOrElse(1) { "" }
        val title = "$home 对阵 $away 回放"
        _ui.value = _ui.value.copy(title = title)
        val r = runCatching { Graph.live.replayLines(home, away) }.getOrNull()
        if (r == null || r.second.isEmpty()) {
            stopForFatal()
            _ui.value = _ui.value.copy(fatalMsg = "暂无该场比赛回放视频", title = title)
            return
        }
        if (this::engine.isInitialized) runCatching { engine.release() }
        installEngine(EngineRegistry.create(settings.current().engineId, Graph.appContext))
        _ui.value = _ui.value.copy(
            title = r.first, epCount = 1, lines = r.second,
            engineId = engine.engineId, engineName = engine.displayName,
            aspectOrdinal = settings.current().aspectOrdinal,
        )
        collectEngineState()
        startSession(0, 0L)
    }

    /** 集锦/已知 vid 直入播放器（原版 playVodDirect：hhkan detail 多线路，默认第 0 集） */
    private suspend fun bootHhkanVid() {
        val vid = vodId.removePrefix("hhkanplay:")
        val lines = runCatching { Graph.live.hhkanVidLines(vid) }.getOrDefault(emptyList())
        if (lines.isEmpty()) {
            stopForFatal()
            _ui.value = _ui.value.copy(fatalMsg = "该内容暂无可用线路")
            return
        }
        if (this::engine.isInitialized) runCatching { engine.release() }
        installEngine(EngineRegistry.create(settings.current().engineId, Graph.appContext))
        _ui.value = _ui.value.copy(
            lines = lines, engineId = engine.engineId, engineName = engine.displayName,
            aspectOrdinal = settings.current().aspectOrdinal,
        )
        collectEngineState()
        startSession(0, 0L)
    }

    /** 线路后台预解析（原版 O2：换线免等）。
     *  v1.10（2026-08-31 低端机优化）：旧版起播瞬间并发解析全部线路——低配 CPU 上
     *  与首段缓冲抢后端/抢带宽，起播反而更慢；改为起播 3s 后串行预解析（逐线 400ms 间隔）。 */
    private fun prefetchLines() {
        if (_ui.value.isLive) return   // 直播线路 url 已是 relay 现成地址，无需预解析
        viewModelScope.launch {
            delay(3_000)
            _ui.value.lines.forEach { line ->
                if (line.playRef.isNotBlank() && line.url.isBlank()) {
                    repo.resolvePlayUrl(line)
                    delay(400)
                }
            }
        }
    }

    /** Reuse 60-second probes on normal opens; explicit retries refresh the server-probed HLS list. */
    private suspend fun bootLive(fresh: Boolean) {
        val matchId = vodId.removePrefix("live:")
        liveMatchId = matchId
        // 标题异步补：matches() 冷抓可能 20s+，绝不能挡在起播链路上。
        viewModelScope.launch {
            val m = runCatching { Graph.live.matches().find { it.matchId == matchId } }.getOrNull()
            if (m != null) _ui.value = _ui.value.copy(title = "${m.home} 对阵 ${m.away}")
        }
        // Clear prior options immediately so a retry never leaves stale lines visible while loading.
        liveSrcs = emptyList()
        liveReferers.clear()
        _ui.value = _ui.value.copy(
            lines = emptyList(), activeLine = 0, isLive = true,
            sourceResolving = true, fatalMsg = null, controlsVisible = false,
        )
        val sources = try {
            Graph.live.liveStreamSources(matchId, fresh = fresh)
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (failure: Exception) {
            OtvLog.w("bootLive 动态线路解析失败 match=$matchId: ${failure.message}")
            emptyList()
        }
        currentCoroutineContext().ensureActive()
        _ui.value = _ui.value.copy(sourceResolving = false)
        OtvLog.i("bootLive match=$matchId probed HLS 信号源=${sources.size} 个")
        if (sources.isEmpty()) {
            OtvLog.w("bootLive 无可用信号 → fatalMsg")
            stopForFatal()
            _ui.value = _ui.value.copy(fatalMsg = "直播信号暂不可用", title = "足球直播", isLive = true)
            scheduleLiveAutoRetry()
            return
        }
        liveSrcs = sources.map { it.source }
        liveReferers.putAll(sources.associate { it.source to it.referer })
        val lines = sources.map { source -> LineInfo(vodId, source.name, source.playbackUrl) }
        val remembered = runCatching { Graph.settings.liveLastSrc() }.getOrNull().orEmpty()
        val selectedSource = remembered.takeIf { it in liveSrcs } ?: liveSrcs.first()
        val selIdx = liveSrcs.indexOf(selectedSource)
        if (remembered.isNotBlank() && remembered !in liveSrcs) {
            runCatching { Graph.settings.rememberLiveLastSrc(selectedSource) }
        }
        // 重试路径重建：先释放旧引擎（否则每次重试泄漏一个 ExoPlayer 实例）
        if (this::engine.isInitialized) runCatching { engine.release() }
        // 调试注入的引擎强制（am start --es otvEngine vlc，测试软解路径用；一次性消费）
        val engineOverride = com.qiubo.optimaltv.DebugNavBus.pendingEngine?.also {
            com.qiubo.optimaltv.DebugNavBus.pendingEngine = null
        }
        installEngine(EngineRegistry.create(engineOverride ?: settings.current().engineId, Graph.appContext))
        _ui.value = _ui.value.copy(
            title = "足球直播", epCount = 1, lines = lines, isLive = true, activeLine = selIdx,
            engineId = engine.engineId, engineName = engine.displayName,
            aspectOrdinal = settings.current().aspectOrdinal,
        )
        collectEngineState()
        startLiveRenewal()
        if (selIdx > 0) {
            OtvLog.i("bootLive：使用上次仍可用的信号源 $selectedSource（第 ${selIdx + 1} 位）")
        }
        startSession(selIdx, 0L, reuseResolvedUrl = true)
    }

    /** IPTV 电视直播分支（v1.13 2026-09-01）：vodId = "iptv:{频道名}|{播放URL}"。
     *  公开 m3u 源原生直连——不走 8090/8091 中继与解密桥（那是足球影视站源的链路）。
     *  起播前 UrlGuard 双重复验：字符串级（列表解析时已过检）+ DNS 解析级（域名指向
     *  内网的投放在此拦截），不合法绝不交给播放器。
     *  引擎失败链复用 isLive 全套设施：DECODING_FAILED 三段回退（软解→libVLC）、
     *  看门狗/卡顿检测、错误态 30s 自动重试；不启用 90s 续签（IPTV 无签名轮换）。 */
    private suspend fun bootIptv() {
        val raw = vodId.removePrefix("iptv:")
        val sep = raw.indexOf('|')
        // 无「|」分隔 = 直达调试通道（am start --es otvDebugVodId "iptv:{url}"）：整串即 URL，
        // 标题回退「电视直播」。有分隔 = 电视页点击（"iptv:{频道名}|{url}"）
        val chName = if (sep >= 0) raw.substring(0, sep) else "电视直播"
        val url = if (sep >= 0) raw.substring(sep + 1) else raw
        OtvLog.i("bootIptv 入口: $chName url=${url.take(90)}")
        _ui.value = _ui.value.copy(title = chName, isLive = true)
        if (url.isBlank()) {
            stopForFatal()
            _ui.value = _ui.value.copy(fatalMsg = "播放地址缺失")
            return
        }
        // 防线一：字符串级（http/https + 环回/私网/保留段字面地址）
        val base = com.qiubo.optimaltv.data.source.UrlGuard.check(url)
        if (base.blocked) {
            OtvLog.w("iptv UrlGuard 拒绝起播（字符串级）：${base.reason} url=${url.take(80)}")
            stopForFatal()
            _ui.value = _ui.value.copy(fatalMsg = "该源地址被安全策略拒绝（${base.reason}）")
            return
        }
        // 防线二：DNS 解析级（域名全部解析到内网 → 拒绝）；解析失败也拒绝（必然播不了）
        val resolved = kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
            com.qiubo.optimaltv.data.source.UrlGuard.assertPublicResolved(url)
        }
        if (resolved.blocked) {
            OtvLog.w("iptv UrlGuard 拒绝起播（解析级）：${resolved.reason} url=${url.take(80)}")
            stopForFatal()
            _ui.value = _ui.value.copy(fatalMsg = "该源域名解析异常，已拒绝播放")
            return
        }
        // v1.16 无感换台：引擎已初始化则复用（prepare 在同一 Player 实例上换源——渲染面/
        // Surface 不重建，旧画面停帧过渡到新首帧，消除换台黑屏闪烁）；仅首播或调试注入
        // 强制引擎时才新建实例（engineEpoch+1 → 电视页视频层换 key 重建渲染视图）。
        // VLC 兜底引擎同样复用（VlcEngine.swapSource 语义在 prepare 路径等效）。
        // 调试注入引擎强制（am start --es otvEngine vlc；一次性消费）——
        // MuMu 类虚拟 GPU 设备 MediaCodec 初始化可能挂死帧管线（不报错、回调不到，
        // 三段回退无从触发，2026-09-01 实测），VLC 自带软解是这类设备的直连通道
        val engineOverrideIptv = com.qiubo.optimaltv.DebugNavBus.pendingEngine?.also {
            com.qiubo.optimaltv.DebugNavBus.pendingEngine = null
        }
        if (engineOverrideIptv != null || !this::engine.isInitialized) {
            if (this::engine.isInitialized) runCatching { engine.release() }
            installEngine(EngineRegistry.create(engineOverrideIptv ?: settings.current().engineId, Graph.appContext))
        }
        // v1.17 IPTV 大缓冲档（用户需求：美国/英国源卡顿）——首次 prepare 前设置生效
        engine.setLargeBufferPreferred(true)
        _ui.value = _ui.value.copy(
            title = chName, epCount = 1,
            lines = listOf(LineInfo(vodId, chName, url)),
            isLive = true,
            engineId = engine.engineId, engineName = engine.displayName,
            engineEpoch = engineEpochCounter,
            aspectOrdinal = settings.current().aspectOrdinal,
        )
        collectEngineState()
        startSession(0, 0L, reuseResolvedUrl = true)
    }

    /**
     * 直播播放中静默续签（v1.7 引入，v1.14 重写）：
     * 每 90s 对当前信号源 fresh 重解析；签名变化时**先探测旧地址健康**——
     * 旧清单仍可拉取（上游轮换常有宽限期）→ 本轮不切，消除「每 ~9 分钟一次
     * ~300ms 热切换冻结」这一可感知卡顿；旧地址已死 → 立即热切换。
     * 检测间隔 90s 由 LoadControl max 缓冲 120s 完整覆盖（死地址放空缓冲前必被换掉）。
     * 暂停中：先做廉价健康检查，仅旧地址已死才解析+静默换源（不自动续播）。
     * 卡顿时 stall 监测经 renewKick 立即触发（不等 90s）。
     */
    private fun startLiveRenewal() {
        renewJob?.cancel()
        renewJob = viewModelScope.launch {
            while (isActive) {
                // 90s 一轮；期间被 kick（直播卡顿）则立刻进入本轮
                kotlinx.coroutines.withTimeoutOrNull(90_000) { renewKick.receive() }
                if (liveMatchId == null) break
                val s = _ui.value
                val idx = s.activeLine
                val src = liveSrcs.getOrNull(idx) ?: continue
                if (!s.isLive || s.fatalMsg != null) continue
                if (s.playState != EnginePlayState.READY && s.playState != EnginePlayState.BUFFERING) continue
                if (!startedThisSession && !awaitingPrepare) continue
                if (!hasEngine()) continue
                val requestGeneration = sessionGen
                val requestEngine = engine
                val requestEngineEpoch = engineEpochCounter
                val requestPlayState = s.playState
                val requestIsPlaying = s.isPlaying
                val playingUrl = livePlayingUrl
                if (playingUrl.isNullOrBlank()) continue
                fun isCurrent(): Boolean {
                    val current = _ui.value
                    return isCurrentPlaybackRequest(
                        requestGeneration, idx, requestEngine, requestEngineEpoch,
                    ) && current.isLive && current.fatalMsg == null &&
                        current.playState == requestPlayState && current.isPlaying == requestIsPlaying &&
                        liveSrcs.getOrNull(idx) == src && livePlayingUrl == playingUrl
                }
                if (s.playState == EnginePlayState.BUFFERING) {
                    val fresh = try {
                        refreshLivePlaybackUrl(src)
                    } catch (cancelled: CancellationException) {
                        throw cancelled
                    } catch (failure: Exception) {
                        null
                    }
                    if (!isCurrent()) continue
                    if (!fresh.isNullOrBlank()) {
                        writeBackLineUrl(idx, fresh)
                        OtvLog.w("live renew：BUFFERING → fresh 同源换地址")
                        requestEngine.swapSource(fresh)
                        livePlayingUrl = fresh
                    }
                    continue
                }
                if (!s.isPlaying) {
                    // 暂停中：先廉价健康检查（不打上游解析）；活着就什么都不做
                    val alive = try {
                        Graph.live.playlistAlive(playingUrl)
                    } catch (cancelled: CancellationException) {
                        throw cancelled
                    } catch (failure: Exception) {
                        true
                    }
                    if (!isCurrent()) continue
                    if (alive) continue
                    val fresh = try {
                        refreshLivePlaybackUrl(src)
                    } catch (cancelled: CancellationException) {
                        throw cancelled
                    } catch (failure: Exception) {
                        null
                    }
                    if (!isCurrent()) continue
                    if (fresh.isNullOrBlank() || fresh == playingUrl) continue
                    writeBackLineUrl(idx, fresh)
                    livePlayingUrl = fresh
                    if (requestEngine.engineId == "media3") {
                        OtvLog.i("live renew：暂停中旧地址已死 → 静默热切换（保持暂停）")
                        requestEngine.swapSource(fresh, autoplay = false)
                    } else {
                        // VLC 重开必自动播放，不静默换；恢复播放停滞由卡顿阶梯接手（会拿到新地址）
                        OtvLog.i("live renew：暂停中旧地址已死（VLC 引擎，恢复时由阶梯重开新地址）")
                    }
                    continue
                }
                val freshUrl = try {
                    refreshLivePlaybackUrl(src)
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (failure: Exception) {
                    null
                }
                if (!isCurrent()) continue
                if (freshUrl.isNullOrBlank()) {
                    OtvLog.w("live renew：src=$src 解析失败（继续播当前流，下轮再试）")
                    continue
                }
                if (freshUrl == playingUrl) continue   // 签名未变化，无需动作
                // v1.14 健康延后：旧地址仍在服务期 → 不切（消除无意义冻结）
                val oldAlive = try {
                    Graph.live.playlistAlive(playingUrl)
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (failure: Exception) {
                    true
                }
                if (!isCurrent()) continue
                if (oldAlive) {
                    OtvLog.i("live renew：签名已变但旧地址仍在服务 → 本轮不切（消除冻结）")
                    continue
                }
                OtvLog.i("live renew：旧地址已死 → 热切换 line=$idx src=$src（buf=${_ui.value.bufferedAheadMs}ms）")
                writeBackLineUrl(idx, freshUrl)
                requestEngine.swapSource(freshUrl)
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

    /** 引擎状态镜像收集（点播/直播共用）；重试重建引擎时先取消旧收集协程 */
    private var engineStateJob: Job? = null

    /**
     * 缓冲态跟踪（2026-09-28）：只做埋点与「缓冲是否仍在增长」判定，
     * 真正的处置由 [LongPlaybackHealthPolicy] 在健康采样里决定。
     *
     * 正常预缓冲也会进 BUFFERING，所以这里不直接升级故障——只有 buffer
     * 停住超过阈值才交给阶梯，避免把正常预缓冲误判成故障而白白换源。
     */
    private fun trackBufferState(isBuffering: Boolean, bufferedAheadMs: Long) {
        val now = android.os.SystemClock.elapsedRealtime()
        if (isBuffering) {
            if (bufferingSince == 0L) {
                bufferingSince = now
                telemetry.emit(PlaybackTelemetry.Phase.BUFFER_START)
            }
        } else if (bufferingSince != 0L) {
            telemetry.emit(PlaybackTelemetry.Phase.BUFFER_END)
            bufferingSince = 0L
        }
        lastBufferedAheadMs = bufferedAheadMs
    }
    private fun collectEngineState() {
        engineStateJob?.cancel()
        if (!hasEngine()) return
        val observedEngine = engine
        engineStateJob = viewModelScope.launch {
            observedEngine.uiState.collect { st ->
                if (!hasEngine() || engine !== observedEngine) return@collect
                // The source/episode may still be resolving while the old engine publishes its
                // terminal state. Ignore it until this session actually prepares or has started.
                if (!startedThisSession && !awaitingPrepare &&
                    (st.state == EnginePlayState.READY || st.state == EnginePlayState.ENDED ||
                        st.state == EnginePlayState.ERROR)
                ) return@collect
                // v1.19 流畅度：控制条隐藏时把进度类字段（positionMs/bufferedAheadMs，
                // ticker 500ms 一变）量化到整秒——StateFlow 按结构相等去重，隐藏期
                // 发射频率 2Hz→1Hz，播放页/电视页整页重组减半（可见时保持 500ms 原速，
                // 进度条依旧平滑）。VM 内部 stall 检测（8×1s 窗口）/进度落盘（5s）精度无损。
                val hide = !_ui.value.controlsVisible
                val posOut = if (hide) st.positionMs / 1_000 * 1_000 else st.positionMs
                val bufOut = if (hide) st.bufferedAheadMs / 1_000 * 1_000 else st.bufferedAheadMs
                _ui.value = _ui.value.copy(
                    playState = st.state,
                    isPlaying = st.isPlaying,
                    positionMs = posOut,
                    durationMs = st.durationMs,
                    bufferedAheadMs = bufOut,
                    speed = st.speed,
                )
                trackBufferState(st.state == EnginePlayState.BUFFERING, st.bufferedAheadMs)
                when {
                    st.state == EnginePlayState.ENDED && startedThisSession && !awaitingPrepare ->
                        handlePlaybackEnded()
                    st.state == EnginePlayState.READY && awaitingPrepare && !startedThisSession ->
                        onSessionStarted(st.positionMs)
                    st.state == EnginePlayState.ERROR && (startedThisSession || awaitingPrepare) -> {
                        OtvLog.e("engine ERROR: ${st.errorMsg}")
                        // 直播解码失败三段回退（v1.8 2026-08-30）：
                        // ① Media3 硬解挂（MuMu/个别盒子 AVC High 8x8 压段）→ 软解优先重建同线重试；
                        // ② 软解也挂（MuMu 全系 MediaCodec 同一 SoftAVCDec 内核）→ 切 libVLC 引擎
                        //    （自带 ffmpeg 软解，任何设备都能解）；
                        // ③ VLC 也报错 → 交回常规失败链。
                        // 均不占换线/同线重解析配额（解码器问题换线也没用）
                        val decodeFail = st.errorMsg?.contains("DECODING_FAILED") == true
                        if (_ui.value.isLive && decodeFail && hasEngine()) {
                            when {
                                !softwareFallbackTried && engine.engineId == "media3" -> {
                                    softwareFallbackTried = true
                                    OtvLog.w("直播硬解失败 → 软解优先重建 + 同线重试")
                                    engine.setSoftwareDecoderPreferred(true)
                                    val cur = _ui.value.activeLine
                                    viewModelScope.launch { startSession(cur, 0L, reuseResolvedUrl = true) }
                                }
                                !vlcFallbackTried && engine.engineId == "media3" -> {
                                    vlcFallbackTried = true
                                    OtvLog.w("直播 MediaCodec 全线失败（硬解+软解）→ 切 libVLC 兜底引擎（自带软解）")
                                    switchEngine("vlc")
                                }
                                else -> onLineFail("引擎错误: ${st.errorMsg}")
                            }
                        } else {
                            onLineFail("引擎错误: ${st.errorMsg}")
                        }
                    }
                }
            }
        }
    }

    /** 点播集结束后自动进入下一集；直播和最后一集保持结束态。 */
    private fun handlePlaybackEnded() {
        if (handledEndedSessionGen == sessionGen) return
        handledEndedSessionGen = sessionGen
        val current = _ui.value
        if (current.isLive) {
            when (PlaybackSessionPolicy.actionAfterLiveEnd(liveEndedRecoveries)) {
                PlaybackSessionPolicy.LiveEndAction.RESTART_CURRENT -> {
                    liveEndedRecoveries++
                    val line = current.activeLine
                    OtvLog.w("直播流提前结束 → 同线 fresh 重启一次（line=$line）")
                    renewJob?.cancel()
                    startedThisSession = false
                    awaitingPrepare = false
                    viewModelScope.launch {
                        startSession(line, 0L)
                        if (liveMatchId != null) startLiveRenewal()
                    }
                }
                PlaybackSessionPolicy.LiveEndAction.STOP -> {
                    OtvLog.w("直播流再次结束 → 停止自动重启，等待用户操作")
                    awaitingPrepare = false
                    stopForFatal()
                    _ui.value = _ui.value.copy(
                        fatalMsg = "直播信号已结束，请手动重试或切换线路",
                        controlsVisible = false,
                        playState = EnginePlayState.IDLE,
                    )
                }
            }
            return
        }
        val next = AutoPlayPolicy.nextEpisodeIndex(
            isLive = current.isLive,
            isMovie = current.isMovie,
            currentIndex = current.epIndex,
            episodeCount = current.epCount,
        ) ?: return
        OtvLog.i("点播自动连播：${current.epIndex + 1} → ${next + 1}")
        switchEpisode(next)
    }

    private suspend fun boot(fresh: Boolean = false) {
        invalidateEpisodeRefresh()
        val request = ++bootGeneration
        // v1.14：冷启动秒进播放器时先等内置后端就绪（最多 ~15s）——backend 未监听时
        // 详情请求连接拒绝被 runCatching 吞掉，直接假性「该集没有可用线路」
        val tBoot = android.os.SystemClock.elapsedRealtime()
        runCatching { repo.awaitBackendReady() }
        if (request != bootGeneration) return
        OtvLog.i("boot 计时：awaitBackendReady ${android.os.SystemClock.elapsedRealtime() - tBoot}ms")
        when {
            // 直播比赛走独立分支（无目录依赖）
            vodId.startsWith("live:") -> { bootLive(fresh = fresh); return }
            // IPTV 电视直播（v1.13）：公开 m3u 源原生直连，无目录依赖
            vodId.startsWith("iptv:") -> { bootIptv(); return }
            // 回放（原版 playRewatch：hhkan 搜索 → 多线路）
            vodId.startsWith("replay:") -> { bootReplay(); return }
            // 集锦/已知 hhkan vid 直入播放器（原版 playVodDirect）
            vodId.startsWith("hhkanplay:") -> { bootHhkanVid(); return }
        }
        _ui.value = _ui.value.copy(sourceStatus = PlayerSourceStatus.LOADING)
        // 等目录就绪（首页已 refresh，一般瞬时）
        var guard = 0
        while (repo.state.value.catalog == null && guard++ < 100) {
            delay(100)
            if (request != bootGeneration) return
        }
        val item = repo.itemById(vodId)
            ?: repo.resolveDetail(vodId)   // 目录外条目（实时搜索结果）兜底
        if (request != bootGeneration) return
        if (item == null) {
            stopForFatal()
            _ui.value = _ui.value.copy(
                fatalMsg = "影片不存在或源未加载",
                sourceStatus = PlayerSourceStatus.ERROR,
            )
            return
        }
        // 懒解析源（hhkan）：先补全选集元数据，vodKey/epCount 才正确
        val effItem = repo.resolveDetail(item.id, forceRefresh = fresh) ?: item
        if (request != bootGeneration) return
        currentItem = effItem
        // 线路构建：走取源阶梯（端侧 detail → L0 last_good → 云端 Resolver 排序）。
        // 云端不可达时自动回落到端侧原序，所以拿到的 lines 与旧版同源同量，
        // 只是顺序可能被 Resolver 优化。
        val resolved = try {
            repo.resolveLinesWithResolver(effItem, epStart)
        } catch (cancelled: kotlinx.coroutines.CancellationException) {
            throw cancelled
        } catch (error: Throwable) {
            if (request != bootGeneration) return
            stopForFatal()
            OtvLog.w("片源查找失败: ${error.message}")
            _ui.value = _ui.value.copy(
                fatalMsg = "片源查找失败，可重试",
                title = effItem.title,
                sourceStatus = PlayerSourceStatus.ERROR,
            )
            return
        }
        if (request != bootGeneration) return
        val lines = VisibleVodLinePolicy.apply(resolved.lines)
        resolveTier = resolved.tier
        resolvePrefillReady = resolved.tier == "L0_last_good"
        val preferResolvedSource = resolved.tier == "L0_last_good" || resolved.tier == "L1_cloud_resolver"
        telemetry.emit(
            PlaybackTelemetry.Phase.RESOLVE_START,
            sourceId = resolved.resolve?.preferred?.sourceId.orEmpty(),
            contentId = resolved.resolve?.preferred?.contentId.orEmpty(),
            note = resolved.tier,
        )
        if (resolved.resolve?.usable == true) {
            val hit = resolved.resolve.preferred
            telemetry.emit(
                PlaybackTelemetry.Phase.RESOLVE_FIRST_SOURCE,
                sourceId = hit?.sourceId.orEmpty(),
                contentId = hit?.contentId.orEmpty(),
            )
        }
        if (lines.isEmpty()) {
            stopForFatal()
            _ui.value = _ui.value.copy(
                fatalMsg = "该集没有可用线路",
                title = effItem.title,
                sourceStatus = PlayerSourceStatus.EMPTY,
            )
            return
        }
        // Pin the detail snapshot only now that this exact ref table will back the visible player.
        repo.acceptPlayRefSnapshots(lines)

        // 线路记忆直选（方案 §3.2-3），否则用设置里的引擎偏好
        val memory = settings.lastLine(vodId)
        val enginePref = memory?.first ?: settings.current().engineId
        // L0 already moved the persisted successful source to index 0; the settings index belongs
        // to the old source ordering and must not override that reordered list.
        val initialLine = if (preferResolvedSource) 0 else memory?.second?.takeIf { it in lines.indices } ?: 0
        val initialLineInfo = lines.getOrNull(initialLine)
        val initialEpisodes = repo.episodesForLine(effItem, initialLineInfo)
            ?.takeIf { epStart in it.indices }
        val playerItem = initialEpisodes?.let { effItem.copy(episodes = it) } ?: effItem
        currentItem = playerItem
        episodeNamesSourceIdentity = repo.episodeSourceIdentity(initialLineInfo)
            ?: repo.defaultEpisodeSourceIdentity(effItem)

        if (this::engine.isInitialized) runCatching { engine.release() }
        installEngine(EngineRegistry.create(enginePref, Graph.appContext))
        // 需求⑨修订：更多面板「选集区仅电影隐藏」。分类优先目录 categoryId
        // （1=电影 2=电视剧 3=动漫 4=综艺 6=短剧）；目录外用 classifyVod 启发式，
        // 另对「集名全是清晰度/语言版本（HD中字/蓝光国语/TC，无集/期/话）」的
        // 2~4 集影片按单集归类——多版本电影不再被集数启发式误判成电视剧
        val isMovie = run {
            val cid = repo.itemById(effItem.id)?.categoryId?.substringAfterLast(':')
            when (cid) {
                "1" -> true
                "2", "3", "4", "6" -> false
                else -> {
                    val versionish = playerItem.episodes.size in 2..4 &&
                        playerItem.episodes.none {
                            it.name.contains("集") || it.name.contains("期") || it.name.contains("话")
                        }
                    com.qiubo.optimaltv.ui.search.classifyVod(
                        playerItem.meta,
                        if (versionish) 1 else playerItem.episodes.size,
                    ) == "电影"
                }
            }
        }
        _ui.value = _ui.value.copy(
            title = playerItem.title,
            epCount = playerItem.episodes.size,
            epNames = playerItem.episodes.map { it.name },
            isMovie = isMovie,
            meta = playerItem.meta.ifBlank {
                (listOf(playerItem.year, playerItem.area).filter { it.isNotBlank() } + playerItem.tags).joinToString(" / ")
            },
            desc = playerItem.desc,
            posterUrl = playerItem.posterUrl,
            lines = lines,
            sourceStatus = PlayerSourceStatus.LOADING,
            engineId = engine.engineId,
            engineName = engine.displayName,
            aspectOrdinal = settings.current().aspectOrdinal,
        )

        // 收集引擎状态镜像（与直播分支共用）
        collectEngineState()

        // 续播位置（方案 §3.4 断点续播；t<1s 不算有效进度）：
        // v1.19 优先用路由带入的跨线路位置（详情页扫全部线路取最近观看）；
        // 无带入（最近观看卡直达/调试注入）走本线路 DB 查询
        val key = playerItem.vodKey(epStart)
        val saved = db.vodDao().getProgress(key)
        if (request != bootGeneration) return
        val dbPos =
            if (saved != null && saved.positionMs > 1_000 &&
                (saved.durationMs <= 0 || saved.positionMs < saved.durationMs - 30_000)
            ) saved.positionMs else 0L
        val startPos = if (resumeStartMs > 1_000) resumeStartMs else dbPos
        if (startPos > 0) Log.i(TAG, "resume $key @${startPos}ms (route=${resumeStartMs > 0})")

        startSession(initialLine, startPos)
        refreshEpisodesForPanel()
    }

    private suspend fun startSession(
        lineIdx: Int,
        startPos: Long,
        reuseResolvedUrl: Boolean = false,
        freshResolve: Boolean = false,
        recreateEngine: Boolean = false,
    ) {
        val isVod = !_ui.value.isLive
        val reuseVODSessionUrl = if (isVod) {
            com.qiubo.optimaltv.playback.ResolvedSessionUrlReusePolicy.vodUrl(
                reuseRequested = reuseResolvedUrl,
                requestedLine = lineIdx,
                activeLine = _ui.value.activeLine,
                sessionUrl = sessionUrl,
            )
        } else null
        if (reuseResolvedUrl && isVod && reuseVODSessionUrl == null) {
            OtvLog.w("拒绝复用非当前点播 URL：line=$lineIdx active=${_ui.value.activeLine} urlReady=${sessionUrl.isNotBlank()}")
            return
        }
        // Do not leave an older episode/source URL eligible while a new VOD session resolves.
        if (isVod) sessionUrl = ""
        // 会话代数（v1.14）：看门狗换线、直连重试、卡顿重解析可能并发触发 startSession，
        // 各自挂起在解析上——旧会话恢复后必须发现自己已被取代并放弃，否则会把引擎
        // 倒灌回旧源（实测：换线 line1 已起播，8s 前的直连重试落地把引擎拽回慢速 line0）
        val gen = ++sessionGen
        startedThisSession = false
        awaitingPrepare = false
        stallRecoveries = 0
        triedLines += lineIdx
        _ui.value = _ui.value.copy(
            activeLine = lineIdx,
            fatalMsg = null,
            sourceStatus = if (_ui.value.isLive) _ui.value.sourceStatus else PlayerSourceStatus.LOADING,
        )
        // 兜底：线路表为空/索引越界时不抛异常（否则协程未捕获直接闪退），走防线三错误态
        val line = _ui.value.lines.getOrNull(lineIdx) ?: run {
            onLineFail("线路缺失（index=$lineIdx）")
            return
        }
        // hhkan 懒解析线路：起播/换线时实时解析直链（与原版行为一致）
        // 直播线路（2026-08-30 稳定性修复）：换线时实时 fresh 重解析该线签名——
        // 上游签名按分钟级轮换，boot 时解析的地址到换线时大概率已作废，
        // 旧逻辑拿旧地址逐线试死 →「全线崩溃看不了」的根因。
        // v1.10：reuseResolvedUrl=true（bootLive 刚解析完的地址）直接复用不再重解析——
        // 旧版「解析完全源再对当前源 fresh 重解析」= 双重解密，低端机起播白等数秒
        var url: String? = null
        if (_ui.value.isLive && liveMatchId != null && !reuseResolvedUrl) {
            liveSrcs.getOrNull(lineIdx)?.let { src ->
                url = runCatching { refreshLivePlaybackUrl(src, line.label) }.getOrNull()
                    ?.also { freshUrl ->
                        if (gen != sessionGen) {
                            OtvLog.w("line $lineIdx 解析期间已有更新会话（gen 落后），放弃")
                            return
                        }
                        _ui.value = _ui.value.copy(
                            lines = _ui.value.lines.toMutableList().also { it[lineIdx] = line.copy(url = freshUrl) },
                        )
                        OtvLog.i("line $lineIdx($src) 换线实时重解析 → 新签名")
                    }
            }
        }
        val playUrl = reuseVODSessionUrl ?: url ?: try {
            // v1.14 点播默认走本地中继（多连接并行拉片对抗 CDN 单连接限速，根治
            // 「20 分钟后频繁卡顿」）；该线路中继已被判不可用时直连
            val preferRelay = line.playRef.isBlank() || line.playRef !in relayBroken
            repo.resolvePlayUrl(line, preferRelay, fresh = freshResolve && !_ui.value.isLive)
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (failure: Exception) {
            if (gen != sessionGen) return
            when (ManualLineSelectionPolicy.resolutionFailureAction(failure)) {
                ResolutionFailureAction.KEEP_SELECTED_LINE -> {
                    Log.w(TAG, "line $lineIdx(${line.label}) fresh 后所选线路/集数不可映射；保留当前线路")
                    stopForSelectedSourceUnavailable(lineIdx)
                    return
                }
                ResolutionFailureAction.USE_EXISTING_FAILOVER -> {
                    onLineFail("线路解析失败（${line.label}）")
                    return
                }
            }
        }
        if (gen != sessionGen) {
            OtvLog.w("line $lineIdx 起播准备期间已有更新会话（gen 落后），放弃旧源")
            return
        }
        if (playUrl.isBlank()) {
            Log.w(TAG, "line $lineIdx(${line.label}) 直链解析失败")
            onLineFail("线路解析失败（${line.label}）")
            return
        }
        sessionViaRelay = playUrl.contains("/api/relay")
        if (_ui.value.isLive) livePlayingUrl = playUrl
        // 直播窗口探测（2026-09-16 长跑卡顿根治）：起播前量一次清单的可播放窗口，
        // 交引擎选目标延迟/缓冲档——中继足球源窗口只有 ~6s，固定「落后 15s」不可达，
        // 速度控制会长期偏离实时并最终掉出滑动窗口（「十几分钟后卡死」的根因）。
        if (_ui.value.isLive && playUrl.contains("m3u8", ignoreCase = true)) {
            val winMs = probeLiveWindowMs(playUrl)
            OtvLog.i("直播窗口探测：${winMs}ms（0=未知，按大窗口档播）url=${playUrl.take(80)}")
            engine.setLiveWindowHintMs(winMs)
        }
        sessionUrl = playUrl
        // v1.21 投屏中：换线/换集不回本地引擎，直接向渲染器重投（进度续投）
        if (_ui.value.castDevice != null) {
            recastToRenderer(playUrl, startPos)
            return
        }
        // Every explicit VOD source selection recreates Media3/VLC to clear a wedged
        // decoder/render pipeline, including a switch to a different source.
        if (recreateEngine && hasEngine()) {
            val engineId = engine.engineId
            runCatching { engine.release() }
            val replacement = EngineRegistry.create(engineId, Graph.appContext)
            replacement.setSpeed(_ui.value.speed)
            installEngine(replacement)
            collectEngineState()
        }
        // VLC 兜底引擎（v1.8）：与 Media3 同走 /api/relay HLS 中继——真实故障流
        // （aarray High 压段）实测该路径 9 分 10 秒零卡顿；后端另有 /api/relayts
        // 连续 TS 端点（实验设施，VLC ts demux 对拼接段时间戳重置容忍差，弃用）
        Log.i(TAG, "session start line=$lineIdx engine=${engine.engineId} url=$playUrl")
        OtvLog.i("startSession line=$lineIdx engine=${engine.engineId} startPos=$startPos url=${playUrl.take(120)}")
        telemetry.emit(PlaybackTelemetry.Phase.PLAYER_PREPARE, note = resolveTier)
        awaitingPrepare = true
        engine.prepare(PrepareRequest(playUrl, startPos))
        armWatchdog()
        armFirstFrameWatchdog()
        startStallMonitor()
        startHealthMonitor()
        prefetchLines()

        // 2026-09-28 快速起播：L0/L1 命中时线路引用已就位，这一步不再有任何片源解析往返
        if (resolvePrefillReady) PlaybackTelemetry.counters.inc("playback.fast_start")
    }

    /**
     * 首帧看门狗（v1.15）：READY 但 15s 内无首帧渲染 = 解码器挂死不报错——
     * MuMu 类虚拟 GPU 设备的 MediaCodec 初始化可能挂死帧管线（无错误回调、位置
     * 缓冲均正常，DECODING_FAILED 回调无从触发，三段回退够不着），实测 CCTV5+
     * 换台黑屏假播放即此形态。此看门狗把该形态接回现有回退链：
     * media3 未试过 VLC → switchEngine("vlc")；VLC 也没有 vout → onLineFail
     * （直播失败链：同线重试 → 错误态 30s 自动重试 + 页面层自动跳台）。
     * 点播不启用（有进度语义，黑屏可由用户 seek 感知，避免误伤慢起播长视频）。
     */
    private var firstFrameJob: Job? = null
    private fun armFirstFrameWatchdog() {
        firstFrameJob?.cancel()
        firstFrameJob = viewModelScope.launch {
            delay(15_000)
            while (isActive) {
                val s = _ui.value
                if (!s.isLive || s.fatalMsg != null) return@launch
                if (s.playState != EnginePlayState.READY) { delay(2_000); continue }
                val ff = if (hasEngine()) engine.uiState.value.firstFrameRendered else true
                if (ff) return@launch
                OtvLog.w("首帧看门狗：READY 15s 无首帧渲染（解码器挂死形态）→ 引擎回退/失败链")
                if (hasEngine() && engine.engineId == "media3" && !vlcFallbackTried) {
                    OtvLog.w("→ 切 libVLC 兜底引擎（自带软解）")
                    switchEngine("vlc")
                } else {
                    onLineFail("画面无输出（首帧超时）")
                }
                return@launch
            }
        }
    }

    /** 防线一：20s 未起播 → 判定失败自动换线（方案 §3.2-1） */
    private fun armWatchdog() {
        watchdogJob?.cancel()
        watchdogJob = viewModelScope.launch {
            Log.d(TAG, "watchdog armed ${START_WATCHDOG_MS}ms")
            delay(START_WATCHDOG_MS)
            if (!startedThisSession) {
                Log.w(TAG, "watchdog fired: no picture in 20s")
                OtvLog.w("watchdog：20s 无画面 → 换线（起播黑屏线索）")
                onLineFail("起播超时（${START_WATCHDOG_MS / 1000}s 无画面）")
            }
        }
    }

    /** 防线二：播放中位置停滞 → 自动重载一次，再犯换线（方案 §3.2-2） */
    private fun startStallMonitor() {
        stallJob?.cancel()
        stallJob = viewModelScope.launch {
            var lastPos = -1L
            var lastRendered = -1
            var ticks = 0
            while (isActive) {
                delay(1_000)
                val s = _ui.value
                val renderedFrames = if (hasEngine()) {
                    runCatching { engine.decoderStats()?.rendered ?: -1 }.getOrDefault(-1)
                } else {
                    -1
                }
                if (s.playState == EnginePlayState.BUFFERING && !userPaused) {
                    ticks++
                    if (ticks >= STALL_TICKS_LIMIT) {
                        ticks = 0
                        recoverPlaybackStall("BUFFERING ${STALL_TICKS_LIMIT}s 无进展")
                    }
                    lastPos = s.positionMs
                    lastRendered = renderedFrames
                } else if (s.playState == EnginePlayState.READY && s.isPlaying) {
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
                }
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
                if (playing && dec != null && lastRendered >= 0) {
                    val advanced = es.positionMs - lastPos
                    if (dec.rendered > lastRendered) frozenTicks = 0
                    else if (advanced >= HEALTH_FREEZE_MIN_ADVANCE_MS) frozenTicks++ else frozenTicks = 0
                } else {
                    frozenTicks = 0
                }
                val positionAdvanced = lastPos >= 0 && es.positionMs - lastPos >= HEALTH_FREEZE_MIN_ADVANCE_MS
                val renderedFramesAdvanced = PlaybackRecoveryCoordinationPolicy.renderedFramesAdvanced(
                    previous = lastRendered,
                    current = dec?.rendered ?: -1,
                )
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
                        onStablePlayback()
                        if (sameLineRetries != 0) {
                            sameLineRetries = 0
                            OtvLog.i("本线稳定播放 60s → 同线重试配额恢复")
                        }
                    }
                } else {
                    healthyTicks = 0
                }
                if (frozenTicks >= HEALTH_FREEZE_TICKS) {
                    frozenTicks = 0
                    val secs = HEALTH_FREEZE_TICKS * HEALTH_TICK_MS / 1000
                    OtvLog.e("画面冻结：位置前进但渲染帧数 ${secs}s 不增 → 交给既有恢复阶梯")
                    PlaybackHealthLog.freeze(healthSnapshot("画面冻结 ${secs}s（时钟前进、无新帧）"))
                    recoverPlaybackStall("画面冻结 ${secs}s（时钟前进、无新帧）")
                } else if (checkBufferHealth(es.state, es.bufferedAheadMs)) {
                    // 缓冲停住且已超阈值：交给恢复阶梯处置（换源或降级引擎）
                    recoverPlaybackStall("缓冲停住 ${bufferingSince}ms 无增长")
                } else if (android.os.SystemClock.elapsedRealtime() - lastHealthLogAt >= HEALTH_LOG_INTERVAL_MS) {
                    lastHealthLogAt = android.os.SystemClock.elapsedRealtime()
                    PlaybackHealthLog.tick(healthSnapshot("5 分钟体检"))
                }
            }
        }
    }

    /** 体检快照取数（主线程；全部 runCatching，取不到就记 -1）。 */
    /**
     * 缓冲健康评估（2026-09-28）。返回 true = 该升级处置了。
     *
     * 判据核心：**buffer 必须结合「是否还在增长」**。ExoPlayer 正常预缓冲同样会进
     * BUFFERING，此时 buffer 在涨就是健康；只有停住不动才是真卡顿。旧口径只看状态，
     * 正常预缓冲会被误判成故障，代价是把用户正在看的线路白白换掉。
     *
     * 冷却期只观测不动手，避免「换源 → 仍在起播 → 又判卡顿 → 再换源」的自激循环。
     */
    private fun checkBufferHealth(state: EnginePlayState, bufferedAheadMs: Long): Boolean {
        if (state != EnginePlayState.BUFFERING || bufferingSince == 0L) return false
        val now = android.os.SystemClock.elapsedRealtime()
        val bufferingMs = now - bufferingSince
        val growing = LongPlaybackHealthPolicy.isGrowing(lastBufferedAheadMs, bufferedAheadMs)
        val inCooldown = healthCooldown.isActive(now)
        val action = LongPlaybackHealthPolicy.actionFor(bufferingMs, growing, inCooldown)
        lastBufferedAheadMs = bufferedAheadMs
        return when (action) {
            LongPlaybackHealthPolicy.Action.PREPARE_BACKUP -> {
                prefetchLines()
                healthCooldown.arm(now, escalationStep = 1)
                false
            }
            LongPlaybackHealthPolicy.Action.SWITCH_BACKUP,
            LongPlaybackHealthPolicy.Action.ENGINE_DEGRADE,
            -> true
            LongPlaybackHealthPolicy.Action.HEALTHY,
            LongPlaybackHealthPolicy.Action.COOLDOWN,
            LongPlaybackHealthPolicy.Action.NONE,
            -> false
        }
    }

    private fun healthSnapshot(note: String): PlaybackHealthSnapshot {
        val s = _ui.value
        val es = if (hasEngine()) engine.uiState.value else com.qiubo.optimaltv.playback.EngineUiState()
        val line = s.lines.getOrNull(s.activeLine)
        return PlaybackHealthSnapshot(
            kind = if (s.isLive) "live" else "vod",
            content = if (s.isLive) s.title else "${s.title} ep${s.epIndex + 1}/${s.epCount}",
            lineIndex = s.activeLine + 1,
            lineLabel = line?.label.orEmpty().ifBlank { "-" },
            lineCount = s.lines.size,
            url = (if (s.isLive) livePlayingUrl else line?.url).orEmpty(),
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
    private suspend fun probeLiveWindowMs(url: String): Long = withContext(Dispatchers.IO) {
        runCatching {
            val req = okhttp3.Request.Builder().url(url).build()
            windowProbeClient.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return@use 0L
                readWindowMs(resp.body?.string().orEmpty())
            }
        }.getOrDefault(0L)
    }

    /** 纯函数：清单文本 → 窗口毫秒（无 EXTINF 视为未知 0）。 */
    private fun readWindowMs(body: String): Long {
        var sum = 0.0
        var n = 0
        for (line in body.lineSequence()) {
            val t = line.trim()
            if (!t.startsWith("#EXTINF", ignoreCase = true)) continue
            val v = t.substringAfter(":", "").substringBefore(",").trim().toDoubleOrNull() ?: continue
            sum += v
            n++
        }
        return if (n > 0) (sum * 1000).toLong() else 0L
    }

    /** Bounded live/VOD recovery ladder shared with the TV client. */
    private fun recoverPlaybackStall(reason: String) {
        val s = _ui.value
        if (!hasEngine() || s.fatalMsg != null) return
        if (!PlaybackRecoveryCoordinationPolicy.canStartRecovery(
                startedThisSession = startedThisSession,
                awaitingPrepare = awaitingPrepare,
                sourceResolving = s.sourceStatus == PlayerSourceStatus.LOADING,
                recoveryInFlight = stallRecoveryJob?.isActive == true,
            )
        ) return

        val requestSessionGeneration = sessionGen
        val requestLine = s.activeLine
        val requestEngine = engine
        val requestEngineEpoch = engineEpochCounter
        val requestToken = ++stallRecoveryGeneration
        val job = viewModelScope.launch(start = CoroutineStart.LAZY) {
            try {
                if (!isCurrentStallRecovery(
                        requestToken, requestSessionGeneration, requestLine, requestEngine, requestEngineEpoch,
                    )
                ) return@launch
                /* 恢复预算：只在一次真实恢复启动时消耗，single-flight 阻止同一故障窗口重复扣配额。 */
                if (!recoveryBudget.requestRecovery(stablePlayback = false)) {
                    OtvLog.e("恢复预算耗尽（${recoveryBudget.spent} 次未稳定起播）→ 放弃：$reason")
                    runCatching { PlaybackHealthLog.stall(healthSnapshot("恢复预算耗尽：$reason")) }
                    telemetry.emit(PlaybackTelemetry.Phase.FATAL, note = "recovery_budget_exhausted")
                    stopForFatal()
                    _ui.value = _ui.value.copy(
                        fatalMsg = "播放持续卡顿，已完成自动恢复",
                        controlsVisible = false,
                    )
                    if (_ui.value.isLive) scheduleLiveAutoRetry()
                    return@launch
                }

                val state = _ui.value
                val kind = if (state.isLive) PlaybackKind.LIVE else PlaybackKind.VOD
                val attempt = recoveryAttempt++
                val action = PlaybackRecoveryPolicy.action(kind, attempt)
                val resume = PlaybackRecoveryPolicy.resumePosition(state.positionMs)
                val current = state.activeLine
                // 长时体检：交给恢复阶梯前先留现场（排查方案 §22 字段清单）
                runCatching { PlaybackHealthLog.stall(healthSnapshot(reason)) }
                OtvLog.w("recovery kind=$kind step=$attempt action=$action reason=$reason pos=${state.positionMs}")
                when (action) {
                    RecoveryAction.RELOAD_CURRENT -> startRecoverySession(
                        requestToken,
                        current,
                        if (state.isLive) 0L else resume,
                        reuseResolvedUrl = true,
                    )
                    RecoveryAction.REFRESH_CURRENT -> {
                        state.lines.getOrNull(current)?.playRef?.takeIf { it.isNotBlank() }?.let(repo::bustPlayCache)
                        startRecoverySession(requestToken, current, if (state.isLive) 0L else resume)
                    }
                    RecoveryAction.SWITCH_SOURCE -> {
                        telemetry.emit(PlaybackTelemetry.Phase.AUTO_SWITCH, note = "step$attempt")
                        // 换源后进入冷却：刚换的线正在起播，缓冲中属正常，此刻再判卡顿会自激。
                        healthCooldown.arm(android.os.SystemClock.elapsedRealtime(), attempt)
                        val next = state.lines.indices.firstOrNull { it != current && it !in triedLines }
                            ?: state.lines.indices.firstOrNull { it != current }
                        if (next != null) {
                            startRecoverySession(requestToken, next, if (state.isLive) 0L else resume)
                        } else {
                            recreateEngineForRecovery(current, resume, requestToken)
                        }
                    }
                    RecoveryAction.RECREATE_ENGINE -> recreateEngineForRecovery(current, resume, requestToken)
                    RecoveryAction.RESET_RUNTIME -> {
                        com.qiubo.optimaltv.playback.StreamDecryptServer.reset()
                        com.qiubo.optimaltv.BackendService.reboot(Graph.appContext, viewModelScope)
                        delay(1_800)
                        startRecoverySession(requestToken, current, 0L)
                    }
                    RecoveryAction.GIVE_UP -> {
                        stopForFatal()
                        _ui.value = state.copy(fatalMsg = "播放持续卡顿，已完成自动恢复", controlsVisible = false)
                        if (state.isLive) scheduleLiveAutoRetry()
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
        resume: Long,
        reuseResolvedUrl: Boolean = false,
    ) {
        if (token != stallRecoveryGeneration) return
        startSession(line, resume, reuseResolvedUrl = reuseResolvedUrl)
        val generation = sessionGen
        // Keep single-flight until READY or the existing 20s watchdog/onLineFail resolves prepare.
        while (token == stallRecoveryGeneration && generation == sessionGen &&
            awaitingPrepare && !startedThisSession && _ui.value.fatalMsg == null
        ) {
            delay(100)
        }
    }

    private suspend fun recreateEngineForRecovery(line: Int, resume: Long, recoveryToken: Int) {
        val id = engine.engineId
        telemetry.emit(PlaybackTelemetry.Phase.ENGINE_FALLBACK, note = id)
        runCatching { engine.release() }
        installEngine(EngineRegistry.create(id, Graph.appContext))
        _ui.value = _ui.value.copy(
            engineId = engine.engineId, engineName = engine.displayName, engineEpoch = engineEpochCounter,
        )
        collectEngineState()
        startRecoverySession(recoveryToken, line, if (_ui.value.isLive) 0L else resume)
    }

    private fun cancelStallRecovery() {
        stallRecoveryGeneration++
        stallRecoveryJob?.cancel()
        stallRecoveryJob = null
    }

    /**
     * 把本集成功的线路写进 last_good（L0 缓存的写侧）。
     *
     * 只存 `playRef` 中间段的稳定线路引用（源 / 内容 / 线路号）——**不存播放地址、
     * Cookie 或签名**，播放地址每次仍由 `/vod/play` 即时解析，签名过期不会污染缓存。
     */
    private fun rememberSuccessfulLine() {
        if (_ui.value.isLive) return
        val line = _ui.value.lines.getOrNull(_ui.value.activeLine) ?: return
        val item = currentItem ?: return
        if (item.mytvId.isBlank() || line.playRef.isBlank()) return
        // A fresh Douban retry may have resolved this unchanged UI ref using a new source ID.
        // Persist the actual middle playRef field used by the successful request.
        val lineId = repo.lineIdForLastGood(line)
        if (lineId.isBlank()) return
        Graph.appScope.launch {
            repo.rememberLastGoodSource(
                mytvId = item.mytvId,
                epIndex = _ui.value.epIndex,
                lineId = lineId,
            )
        }
    }

    /**
     * 这条线**真的撑住了**（健康监控连续 60s 在播）——恢复冷却窗与恢复预算。
     *
     * 这是唯一有资格清 [healthCooldown] / [recoveryBudget] 的信号。
     * 起播成功（哪怕只有一帧）不算数：僵尸源的特征恰好就是「能出画面、几秒后必断」，
     * 拿起播当信号就等于给无限循环发通行证。
     */
    private fun onStablePlayback() {
        if (healthCooldown.until == 0L && recoveryBudget.spent == 0) return
        val wasCooling = healthCooldown.until
        healthCooldown.onStablePlayback()
        recoveryBudget.onStablePlayback()
        OtvLog.i("本线稳定播放 60s → 冷却窗与恢复预算恢复（wasCooldown=$wasCooling）")
    }

    private suspend fun onSessionStarted(posMs: Long) {
        startedThisSession = true
        awaitingPrepare = false
        if (!_ui.value.isLive) {
            _ui.value = _ui.value.copy(sourceStatus = PlayerSourceStatus.READY)
        }
        // 冷却**不在这里清**：本方法在新线一 READY 就跑，而换线刚装过弹——
        // 清掉等于「刚换的线正在起播、缓冲属正常」这段最需要保护的窗口当场作废，
        // 紧接着的健康采样会再判卡顿、再换线，自激循环。真正清冷却的地方是
        // 稳定播放满 60s（onStablePlayback）——那时才值得重新完全信任这条线。
        bufferingSince = 0L
        lastBufferedAheadMs = 0L
        // 首帧达成：结算端到端耗时。此后端到端值不再变化，可安全上报表。
        telemetry.emit(PlaybackTelemetry.Phase.FIRST_FRAME, note = resolveTier)
        PlaybackTelemetry.counters.inc("playback.first_frame")
        rememberSuccessfulLine()
        watchdogJob?.cancel()
        liveAutoRetries = 0   // 成功起播：自动重试计数归零（下次故障重新计 5 次）
        liveAutoRetryJob?.cancel()
        /* 2026-09-16 僵尸源修复：起播成功【不】立刻恢复同线重试配额。
           线上实测：某备用信号源「能出画面、几秒后必断」，每次短暂 READY 都把配额清零，
           于是 3 次重试永远用不完 → App 死守这条死源无限循环（日志里 rbuf 计数一路涨到
           400+，观感=一直卡），永远不会换到能用的源。配额改由「稳定播放满 60s」恢复
           （见 startHealthMonitor），僵尸源撑不到 60s，第 4 次就会换线。 */
        Log.i(TAG, "READY line=${_ui.value.activeLine} @$posMs ms")
        OtvLog.i("READY line=${_ui.value.activeLine} @$posMs ms（出画面）")
        // 长时体检基线：起播现场（30s 验收与长跑对比都以这条为参考）
        runCatching { PlaybackHealthLog.start(healthSnapshot("起播")) }
        if (_ui.value.isLive) armIptvAudioCodecCheck()   // 电视#8：无声编解码检查
        // 线路记忆（仅点播；足球线路按当前探活列表恢复上次选择）
        if (!_ui.value.isLive) {
            runCatching { settings.rememberLine(vodId, engine.engineId, _ui.value.activeLine) }
        }
        if (_ui.value.isLive || isFootballVod) return  // 直播/足球回放不进观看历史/进度库
        // 历史
        currentItem?.let { item ->
            db.vodDao().upsertHistory(
                HistoryEntity(
                    vodId = item.id,
                    title = item.title,
                    posterUrl = item.posterUrl,
                    epIndex = epStart,
                    positionMs = posMs,
                    durationMs = _ui.value.durationMs,
                    updatedAt = System.currentTimeMillis(),
                ),
            )
        }
        startProgressSaverIfNeeded()
    }

    /** 电视#8 无声源治理：IPTV 起播后检查音频编解码——Media3 软解不含 AC-3/EAC-3/DTS
     *  时音频渲染器静默失败（画面正常、无报错、无声）。检测到即切 libVLC（自带
     *  ffmpeg 软解全编解码）重起同线；仅 media3 引擎且每会话一次（VLC 自身不查）。 */
    private var audioCodecChecked = false
    private fun armIptvAudioCodecCheck() {
        if (audioCodecChecked || !vodId.startsWith("iptv:") || !hasEngine() || engine.engineId != "media3") return
        audioCodecChecked = true
        viewModelScope.launch {
            delay(1_500)   // 等音频轨道完成选择（READY 即刻查 codecs 常为 null）
            if (!hasEngine() || engine.engineId != "media3") return@launch
            val codecs = runCatching { engine.audioCodecs() }.getOrNull()
            if (codecs != null && Regex("ac-3|eac-3|ec-3|dts", RegexOption.IGNORE_CASE).containsMatchIn(codecs)) {
                OtvLog.w("iptv 音频编解码不可解（$codecs，画面正常但无声）→ 切 libVLC 重起")
                switchEngine("vlc")
            } else {
                OtvLog.i("iptv 音频编解码: ${codecs ?: "未选中"}")
            }
        }
    }

    private fun startProgressSaverIfNeeded() {
        if (progressJob?.isActive == true) return
        progressJob = viewModelScope.launch {
            while (isActive) {
                delay(PROGRESS_INTERVAL_MS)
                saveProgress(force = false)
            }
        }
    }

    private suspend fun saveProgress(force: Boolean, snapshot: PlayerUiState = _ui.value) {
        val s = snapshot
        if (s.isLive || isFootballVod) return  // 直播/足球回放不落进度库
        val item = currentItem ?: return
        // v1.21 投屏中：以渲染器上报进度为准（本地引擎已暂停，positionMs 是旧值）
        if (s.castDevice != null) {
            if (s.castDurationMs > 0 && (force || s.castPositionMs > 1_000)) {
                db.vodDao().upsertProgress(
                    ProgressEntity(
                        vodKey = item.vodKey(s.epIndex),
                        vodId = item.id,
                        epIndex = s.epIndex,
                        positionMs = s.castPositionMs,
                        durationMs = s.castDurationMs,
                        updatedAt = System.currentTimeMillis(),
                    ),
                )
                db.vodDao().upsertHistory(
                    HistoryEntity(
                        vodId = item.id,
                        title = item.title,
                        posterUrl = item.posterUrl,
                        epIndex = s.epIndex,
                        positionMs = s.castPositionMs,
                        durationMs = s.castDurationMs,
                        updatedAt = System.currentTimeMillis(),
                    ),
                )
            }
            return
        }
        val valid = s.playState == EnginePlayState.READY && s.durationMs > 0 &&
                (force || (s.isPlaying && s.positionMs > 1_000))
        if (!valid) return
        db.vodDao().upsertProgress(
            ProgressEntity(
                vodKey = item.vodKey(s.epIndex),
                vodId = item.id,
                epIndex = s.epIndex,
                positionMs = s.positionMs,
                durationMs = s.durationMs,
                updatedAt = System.currentTimeMillis(),
            ),
        )
        // 需求 我的#1/影视#6：观看历史同步实时位置与总时长——旧版只在起播时写一次历史
        // （position=起始位置、duration=0），「最近观看/历史记录」的进度条永远为空、时间停在开头
        db.vodDao().upsertHistory(
            HistoryEntity(
                vodId = item.id,
                title = item.title,
                posterUrl = item.posterUrl,
                epIndex = s.epIndex,
                positionMs = s.positionMs,
                durationMs = s.durationMs,
                updatedAt = System.currentTimeMillis(),
            ),
        )
    }

    /** 失败 → 自动换下一条未试过的线路；全试过 → ErrorWidget 兜底（防线三）。
     *  直播特例：当前源只 fresh 重解析一次，仍失败就切换未尝试的备用源，
     *  所有源都失败后才进错误态，避免某条线路短暂失效拖死整场比赛。 */
    private var sameLineRetries = 0
    /** 直播软解兜底已尝试（每 VM 一次） */
    private var softwareFallbackTried = false
    /** 直播 libVLC 兜底已尝试（每 VM 一次；VLC 也失败才认输走错误态） */
    private var vlcFallbackTried = false

    /** 直播解码失败引擎切换（v1.8）：释放当前引擎 → 建 VLC → 同线重起。
     *  UI 层按 engineId key 重建渲染视图（PlayerScreen），因此必须先改 _ui 再起播。 */
    private fun switchEngine(targetId: String) {
        if (!hasEngine()) return
        runCatching { engine.release() }
        installEngine(EngineRegistry.create(targetId, Graph.appContext))
        _ui.value = _ui.value.copy(
            engineId = engine.engineId, engineName = engine.displayName, engineEpoch = engineEpochCounter,
        )
        collectEngineState()
        val cur = _ui.value.activeLine
        viewModelScope.launch { startSession(cur, 0L, reuseResolvedUrl = true) }
    }

    /**
     * v1.16 IPTV 多源备份轮换：当前线路失败 → 同频道下一备用线路热切换（复用引擎，
     * 渲染面停帧过渡，无黑屏重建）。返回 true = 已接管；备用耗尽返回 false 落入直播
     * 常规失败链（同线重试 → 错误态 30s 自动重试 + 页面层自动跳台）。
     */
    private fun iptvRotateLine(reason: String): Boolean {
        if (vodId.isEmpty() || !vodId.startsWith("iptv:") || iptvBackups.isEmpty()) return false
        val name = vodId.removePrefix("iptv:").substringBefore('|')
        val next = iptvBackups.removeFirst()
        vodId = "iptv:$name|$next"
        sameLineRetries = 0
        OtvLog.w("iptv 线路失败($reason) → 备用线路热切换（剩 ${iptvBackups.size} 条）url=${next.take(80)}")
        _ui.value = _ui.value.copy(
            lines = listOf(LineInfo(vodId, name, next)), activeLine = 0, fatalMsg = null,
        )
        viewModelScope.launch { startSession(0, 0L, reuseResolvedUrl = true) }
        return true
    }

    /**
     * v1.16 IPTV 重试从主线路整队重启：备用线路耗尽进错误态后，30s 自动重试/手动重试
     * 若仍播「最后一条已证死的备用线」必然再失败——重置为主线路 + 恢复完整备用队列
     * （源可能已恢复），与整页重进等价但不丢电视页内嵌会话。
     */
    private fun iptvRestartFromPrimary() {
        val p = iptvPrimary ?: run { viewModelScope.launch { boot(fresh = true) }; return }
        vodId = "iptv:${p.first}|${p.second}"
        iptvBackups.clear()
        iptvBackups.addAll(p.third)
        triedLines.clear()
        sameLineRetries = 0
        softwareFallbackTried = false
        vlcFallbackTried = false
        OtvLog.i("iptv 重试：回到主线路 ${p.first}（备用 ${p.third.size} 条恢复）")
        _ui.value = _ui.value.copy(fatalMsg = null, title = p.first, isLive = true)
        viewModelScope.launch { bootIptv() }
    }

    private fun onLineFail(reason: String) {
        if (_ui.value.fatalMsg != null) return
        awaitingPrepare = false
        // 2026-09-28 故障分类：解码类换引擎、网络类换源。方向判错代价很高——
        // 解码失败反复换源只是把同一条坏线路换着花样重试，永远出不了画面。
        val remedy = FirstFrameRecoveryPolicy.remedyFor(reason)
        telemetry.emit(
            PlaybackTelemetry.Phase.FATAL,
            note = FirstFrameRecoveryPolicy.classify(reason).name.lowercase(),
        )
        // v1.16：IPTV 频道内备用线路优先（换线路 ≠ 换频道，观众无感）
        if (iptvRotateLine(reason)) return
        if (_ui.value.isLive) {
            // 解码类失败重解析无意义（URL 本身有效，是解码器不行）——直进错误态
            // 等 30s 自动重试。解码类错误不是 URL 过期，避免同线密集重解析触发上游限流。
            if (reason.contains("DECODING_FAILED")) {
                OtvLog.w("line fail(${_ui.value.activeLine}): 解码失败（重解析无效）→ 错误态等 30s 自动重试")
                viewModelScope.launch { saveProgress(force = false) }
                stopForFatal()
                _ui.value = _ui.value.copy(fatalMsg = reason, controlsVisible = false)
                scheduleLiveAutoRetry()
                return
            }
            val cur = _ui.value.activeLine
            val hasAlternate = _ui.value.lines.indices.any { it != cur && it !in triedLines }
            when (PlaybackRecoveryPolicy.liveLineFailureAction(sameLineRetries, hasAlternate)) {
                RecoveryAction.REFRESH_CURRENT -> {
                    sameLineRetries++
                    OtvLog.w("line fail($cur): $reason → 同线 fresh 重解析（仅重试一次）")
                    viewModelScope.launch { startSession(cur, 0L) }
                }
                RecoveryAction.SWITCH_SOURCE -> {
                    sameLineRetries = 0
                    val next = _ui.value.lines.indices.firstOrNull { it != cur && it !in triedLines }
                    if (next == null) {
                        stopForFatal()
                        _ui.value = _ui.value.copy(fatalMsg = reason, controlsVisible = false)
                        scheduleLiveAutoRetry()
                    } else {
                        OtvLog.w("line fail($cur): $reason → 切换备用源 $next")
                        viewModelScope.launch { startSession(next, 0L) }
                    }
                }
                else -> {
                    sameLineRetries = 0
                    OtvLog.w("line fail($cur): $reason → 所有直播源失败，进入错误态")
                    viewModelScope.launch { saveProgress(force = false) }
                    stopForFatal()
                    _ui.value = _ui.value.copy(fatalMsg = reason, controlsVisible = false)
                    // 直播：错误态 30s 自动重试（fresh）——未开赛/上游抖动不用用户守着重试
                    scheduleLiveAutoRetry()
                }
            }
            return
        }
        sameLineRetries = 0
        // v1.14 点播阶梯第一级：中继路径失败 → 同线直连重试一次。
        // python 网络栈被 CDN 拒（防盗链/TLS 指纹）≠ ExoPlayer 直连也不行；
        // 直连救活的线路记入 relayBroken，本会话后续直接走直连不再撞墙。
        if (!_ui.value.isLive) {
            val curLine = _ui.value.lines.getOrNull(_ui.value.activeLine)
            if (curLine != null && curLine.playRef.isNotBlank() &&
                curLine.playRef !in relayBroken && sessionViaRelay
            ) {
                relayBroken += curLine.playRef
                repo.bustPlayCache(curLine.playRef)
                OtvLog.w("line fail(${_ui.value.activeLine}): 中继路径失败 → 同线直连重试")
                val pos = _ui.value.positionMs
                viewModelScope.launch {
                    startSession(_ui.value.activeLine, if (pos > 1_000) pos else 0L)
                }
                return
            }
        }
        val next = _ui.value.lines.indices.firstOrNull { it !in triedLines }
        Log.w(TAG, "line fail(${_ui.value.activeLine}): $reason → ${next ?: "no more lines"}")
        OtvLog.w("line fail(${_ui.value.activeLine}): $reason → ${next?.let { "换线 $it" } ?: "全部失败"}")
        /* 解码类故障：换线救不了（URL 有效，是本机解不动）。libVLC 自带 ffmpeg
           软解全编解码，是这类故障的正解；先用它重起同线，仍失败再进常规换线阶梯。 */
        if (remedy == FirstFrameRecoveryPolicy.Remedy.SWITCH_ENGINE &&
            hasEngine() && engine.engineId != "vlc" && !vlcFallbackTried
        ) {
            vlcFallbackTried = true
            OtvLog.w("line fail(${_ui.value.activeLine}): 解码类故障 → 切 libVLC 重起同线")
            telemetry.emit(PlaybackTelemetry.Phase.ENGINE_FALLBACK, note = "vlc")
            val pos = _ui.value.positionMs
            val cur = _ui.value.activeLine
            viewModelScope.launch {
                if (hasEngine()) engine.release()
                installEngine(EngineRegistry.create("vlc", Graph.appContext))
                _ui.value = _ui.value.copy(
                    engineId = engine.engineId, engineName = engine.displayName,
                    engineEpoch = engineEpochCounter,
                )
                collectEngineState()
                startSession(cur, if (pos > 1_000) pos else 0L, reuseResolvedUrl = true)
            }
            return
        }
        if (next == null) {
            viewModelScope.launch { saveProgress(force = false) }
            stopForFatal()
            _ui.value = _ui.value.copy(fatalMsg = reason, controlsVisible = false)
        } else {
            val pos = _ui.value.positionMs
            viewModelScope.launch { startSession(next, if (pos > 1_000) pos else 0L) }
        }
    }

    // ---------- 用户操作 ----------

    fun retry() {
        cancelStallRecovery()
        recoveryAttempt = 0
        // 需求 足球#2：线路为空（如直播信号未解析出）时 startSession 会数组越界闪退——
        // 此时重试 = 重新走 boot 全链路（重新解析信号源），而不是按线路索引起播。
        // 直播重试同走 fresh（直播稳定性方案修复 2）：服务端 STREAM_CACHE 会把成功结果
        // 缓存 300s，普通重试拿回的还是那个已过期/已受限的 URL → 「信号尚未开播」死循环
        if (_ui.value.lines.isEmpty() || _ui.value.isLive) {
            triedLines.clear()
            liveAutoRetries = 0   // 手动重试重置自动重试配额
            liveEndedRecoveries = 0
            // v1.16 IPTV：手动重试从主线路整队重启（见 iptvRestartFromPrimary）
            if (vodId.startsWith("iptv:") && iptvPrimary != null) {
                iptvRestartFromPrimary()
                return
            }
            _ui.value = _ui.value.copy(
                fatalMsg = null,
                sourceStatus = if (_ui.value.isLive) _ui.value.sourceStatus else PlayerSourceStatus.LOADING,
            )
            launchBoot(fresh = true)
            return
        }
        val line = _ui.value.activeLine
        val pos = if (_ui.value.castDevice != null) _ui.value.castPositionMs else _ui.value.positionMs
        triedLines.clear()
        // 手动重试 = 用户明确再给一次机会：恢复预算与冷却窗归零，
        // 否则刚点完「重试」就立刻撞上上一轮攒下的预算上限，观感像按钮没生效。
        recoveryBudget.reset()
        healthCooldown.reset()
        if (hasEngine()) runCatching { engine.pause() }
        _ui.value = _ui.value.copy(
            fatalMsg = null,
            playState = EnginePlayState.BUFFERING,
            sourceStatus = if (_ui.value.isLive) _ui.value.sourceStatus else PlayerSourceStatus.LOADING,
        )
        launchManualSession(line, if (pos > 1_000) pos else 0L, recreateEngine = !_ui.value.isLive)
    }

    fun switchLine(idx: Int) {
        val state = _ui.value
        if (!ManualLineSelectionPolicy.shouldStart(state.lines.size, idx, state.activeLine, state.isLive)) return
        recoveryAttempt = 0
        // 手动切源 = 用户重选起点：清空换线尝试记录——旧记录会把此前起播过的线路
        // 当「已试过」跳过，避免换线尝试记录误跳过此前正常播放过的线路。
        if (hasEngine()) engine.pause()
        _ui.value = _ui.value.copy(
            playState = EnginePlayState.BUFFERING,
            fatalMsg = null,
            sourceStatus = if (state.isLive) state.sourceStatus else PlayerSourceStatus.LOADING,
        )
        triedLines.clear()
        sameLineRetries = 0
        // 需求③（2026-09-07）：直播手动选源 → 持久化，下次进直播默认直选该源
        if (_ui.value.isLive) {
            liveSrcs.getOrNull(idx)?.let { src ->
                viewModelScope.launch {
                    runCatching { Graph.settings.rememberLiveLastSrc(src) }
                        .onSuccess { OtvLog.i("直播源记忆已更新: $src") }
                }
            }
        }
        val pos = if (_ui.value.castDevice != null) _ui.value.castPositionMs else _ui.value.positionMs
        touch()
        launchManualSession(idx, if (state.isLive) 0L else if (pos > 1_000) pos else 0L, recreateEngine = true)
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

    // ---- v1.21 DLNA 投屏 ----

    /** 扫描局域网渲染器（投屏浮层打开/刷新时调用） */
    fun scanCastDevices() {
        if (_ui.value.castScanning) return
        _ui.value = _ui.value.copy(castScanning = true, castDevices = emptyList())
        viewModelScope.launch {
            val devices = DlnaDiscovery.scan()
            _ui.value = _ui.value.copy(castScanning = false, castDevices = devices)
        }
    }

    /** 开始投屏：当前线路 URL 改写局域网地址 → SetAVTransportURI+Play → 本地引擎暂停让位 */
    fun startCast(dev: com.qiubo.optimaltv.data.cast.DlnaDevice) {
        viewModelScope.launch {
            val url = com.qiubo.optimaltv.data.cast.CastNet.castableUrl(sessionUrl)
            if (url == null) {
                _ui.value = _ui.value.copy(castError = "无局域网连接，无法投屏")
                return@launch
            }
            val pos = if (_ui.value.isLive) 0L else _ui.value.positionMs
            val ok = DlnaControl.setUriAndPlay(dev, url, _ui.value.title)
            if (!ok) {
                _ui.value = _ui.value.copy(castError = "投屏失败（${dev.name}），请重试或换设备")
                return@launch
            }
            if (pos > 3_000) DlnaControl.seek(dev, pos / 1000)
            // 本地让位：暂停 + 撤看门狗/续签循环（防卡顿检测在投屏期间误触发换线；
            // 电视经局域网消费中继流，签名轮换由中继自愈，不需要 app 侧续签）
            runCatching { engine.pause() }
            watchdogJob?.cancel()
            stallJob?.cancel()
            firstFrameJob?.cancel()
            renewJob?.cancel()
            _ui.value = _ui.value.copy(castDevice = dev, controlsVisible = true)
            OtvLog.i("投屏开始 → ${dev.name} url=${url.take(90)}")
            startCastPoll(dev)
        }
    }

    /** 渲染器重投（投屏中换线/换集：URL 已轮换，进度续投） */
    private suspend fun recastToRenderer(playUrl: String, startPos: Long) {
        val dev = _ui.value.castDevice ?: return
        val url = com.qiubo.optimaltv.data.cast.CastNet.castableUrl(playUrl)
        if (url == null) {
            _ui.value = _ui.value.copy(
                castError = "局域网已断开，投屏中止",
                sourceStatus = if (_ui.value.isLive) _ui.value.sourceStatus else PlayerSourceStatus.ERROR,
            )
            stopCast(resumeLocal = true)
            return
        }
        if (!DlnaControl.setUriAndPlay(dev, url, _ui.value.title)) {
            _ui.value = _ui.value.copy(
                castError = "换线投屏失败（${dev.name}）",
                sourceStatus = if (_ui.value.isLive) _ui.value.sourceStatus else PlayerSourceStatus.ERROR,
            )
            return
        }
        if (startPos > 3_000) DlnaControl.seek(dev, startPos / 1000)
        if (!_ui.value.isLive) {
            _ui.value = _ui.value.copy(sourceStatus = PlayerSourceStatus.READY)
        }
        OtvLog.i("投屏换线 → ${dev.name} line=${_ui.value.activeLine}")
    }

    /** 渲染器进度轮询（2s；断连三次自动停止投屏） */
    private fun startCastPoll(dev: com.qiubo.optimaltv.data.cast.DlnaDevice) {
        castPollJob?.cancel()
        castPollJob = viewModelScope.launch {
            var fails = 0
            while (isActive && _ui.value.castDevice?.uuid == dev.uuid) {
                delay(2_000)
                val pos = DlnaControl.position(dev)
                if (pos == null) {
                    if (++fails >= 3 && _ui.value.castDevice?.uuid == dev.uuid) {
                        OtvLog.w("投屏进度查询连续失败 ×3 → 断开投屏回本地")
                        _ui.value = _ui.value.copy(castError = "与 ${dev.name} 的连接已断开")
                        stopCast(resumeLocal = true)
                        return@launch
                    }
                    continue
                }
                fails = 0
                _ui.value = _ui.value.copy(
                    castPositionMs = pos.first * 1000, castDurationMs = pos.second * 1000)
                saveProgress(force = false)
            }
        }
    }

    /** 断开投屏：Stop 渲染器 → resumeLocal=true 时本地引擎从投屏进度续播 */
    fun stopCast(resumeLocal: Boolean) {
        val dev = _ui.value.castDevice
        castPollJob?.cancel()
        _ui.value = _ui.value.copy(castDevice = null, castPositionMs = 0, castDurationMs = 0)
        if (dev != null) {
            viewModelScope.launch { runCatching { DlnaControl.stop(dev) } }
            OtvLog.i("投屏断开 ← ${dev.name} resumeLocal=$resumeLocal")
        }
        if (resumeLocal && dev != null) {
            val pos = if (_ui.value.isLive) 0L else _ui.value.castPositionMs
            viewModelScope.launch { startSession(_ui.value.activeLine, pos) }
        }
    }

    /** 一次性消费投屏错误提示 */
    fun clearCastError() {
        if (_ui.value.castError != null) _ui.value = _ui.value.copy(castError = null)
    }

    fun togglePlay() {
        // v1.21：投屏中播放/暂停改道渲染器（本地引擎已暂停让位）
        _ui.value.castDevice?.let { dev ->
            viewModelScope.launch {
                if (_ui.value.castDurationMs > 0 &&
                    castPausedAt >= 0 && _ui.value.castPositionMs >= castPausedAt - 1500 &&
                    _ui.value.castPositionMs <= castPausedAt + 1500
                ) {
                    DlnaControl.play(dev)
                    castPausedAt = -1L
                } else {
                    // 无法可靠判断暂停态（部分渲染器无 TransportState 轮询）：
                    // 记录当前位置发 Pause；用户再按且位置未动 → 恢复播放
                    DlnaControl.pause(dev)
                    castPausedAt = _ui.value.castPositionMs
                }
            }
            return
        }
        if (!hasEngine()) return
        if (_ui.value.isPlaying) {
            userPaused = true
            engine.pause()
        } else {
            userPaused = false
            engine.play()
        }
        viewModelScope.launch { saveProgress(force = true) }
    }

    fun seekBy(deltaMs: Long) {
        // v1.21：投屏中 seek 改道渲染器
        _ui.value.castDevice?.let { dev ->
            viewModelScope.launch {
                DlnaControl.seek(dev, (_ui.value.castPositionMs + deltaMs).coerceAtLeast(0) / 1000)
            }
            touch()
            return
        }
        if (!hasEngine()) return
        val s = _ui.value
        if (s.durationMs <= 0) return
        // v1.19 崩溃修复：durationMs ∈ (0,1000)（异常流/直播短暂上报）时旧区间为
        // 0..负数，coerceIn 在 min>max 时抛 IllegalArgumentException 直接闪退
        val maxMs = (s.durationMs - 1_000).coerceAtLeast(0)
        engine.seekTo((s.positionMs + deltaMs).coerceIn(0, maxMs))
        touch()
    }

    /** 绝对位置跳转（移动版进度条拖动/点轨道跳转调用；TV 版暂未使用） */
    fun seekTo(targetMs: Long) {
        // v1.21：投屏中 seek 改道渲染器
        _ui.value.castDevice?.let { dev ->
            viewModelScope.launch { DlnaControl.seek(dev, targetMs.coerceAtLeast(0) / 1000) }
            touch()
            return
        }
        if (!hasEngine()) return
        val s = _ui.value
        if (s.durationMs <= 0) return
        val maxMs = (s.durationMs - 1_000).coerceAtLeast(0)
        engine.seekTo(targetMs.coerceIn(0, maxMs))
        touch()
    }

    /** 数字键跳转百分比（方案 §3.4 键位表）：单键=p*10%，双键组合 */
    fun inputDigit(d: Int) {
        digitBuffer += d.toString()
        digitJob?.cancel()
        digitJob = viewModelScope.launch {
            delay(1_200)
            applyPercent(digitBuffer.toIntOrNull() ?: return@launch)
            digitBuffer = ""
        }
        touch()
    }

    private fun applyPercent(p: Int) {
        val dur = _ui.value.durationMs
        if (dur <= 0 || p !in 0..99) return
        engine.seekTo(dur * p / 100)
    }

    fun cycleSpeed() {
        if (!hasEngine()) return
        val cur = SPEED_STEPS.indexOfFirst { kotlin.math.abs(it - _ui.value.speed) < 0.01f }
        val next = SPEED_STEPS[((cur + 1).mod(SPEED_STEPS.size))]
        setSpeed(next)
    }

    /** 更多面板倍速直选 */
    fun setSpeed(v: Float) {
        if (!hasEngine()) return
        engine.setSpeed(v)
        viewModelScope.launch { settings.setSpeed(v) }
        touch()
    }

    /** 长按快进手势进行标记（移动端 2026-09-21）；倍速恢复基准在按下瞬间取样 */
    private var speedBoostActive = false
    private var speedBeforeBoost = 1f

    /**
     * 长按画面左/右任一侧：临时提到 [PlaybackPolicy.LONG_PRESS_SPEED]，松手由 [endSpeedBoost] 还原。
     * 直播不可变速（与网页版「直播不渲染倍速区」同口径）；只动引擎倍速，不写 settings——
     * 「按住的那一下」不该被记成用户偏好。
     */
    fun beginSpeedBoost() {
        if (speedBoostActive || !hasEngine() || _ui.value.isLive) return
        speedBeforeBoost = _ui.value.speed.takeIf { it > 0.01f } ?: 1f
        speedBoostActive = true
        _ui.value = _ui.value.copy(speedBoost = PlaybackPolicy.LONG_PRESS_SPEED)
        engine.setSpeed(PlaybackPolicy.LONG_PRESS_SPEED)
        touch()
    }

    /** 松手/手势取消：清掉提示并回到用户原倍速（幂等，未按住时直接返回） */
    fun endSpeedBoost() {
        if (!speedBoostActive) return
        speedBoostActive = false
        _ui.value = _ui.value.copy(speedBoost = null)
        if (hasEngine()) engine.setSpeed(speedBeforeBoost)
        touch()
    }

    fun cycleAspect() {
        val nextOrd = (_ui.value.aspectOrdinal + 1).mod(AspectMode.entries.size)
        setAspect(nextOrd)
    }

    /** 更多面板画幅直选 */
    fun setAspect(ord: Int) {
        if (ord !in AspectMode.entries.indices) return
        _ui.value = _ui.value.copy(aspectOrdinal = ord)
        viewModelScope.launch { settings.setAspect(ord) }
        touch()
    }

    fun toggleControls() {
        _ui.value = _ui.value.copy(controlsVisible = !_ui.value.controlsVisible, panel = Panel.NONE)
        touch()
    }

    /** 显示控制条（需求 影视#8：隐藏态左右快进自动弹出菜单栏） */
    fun showControls() {
        if (!_ui.value.controlsVisible) {
            _ui.value = _ui.value.copy(controlsVisible = true, panel = Panel.NONE)
        }
        touch()
    }

    fun hideControls() {
        _ui.value = _ui.value.copy(controlsVisible = false)
    }

    fun setPanel(p: Panel) {
        val previous = _ui.value
        _ui.value = previous.copy(panel = p)
        if (p == Panel.MORE && previous.panel != Panel.MORE && !previous.isLive) refreshEpisodesForPanel()
        touch()
    }

    private fun invalidateEpisodeRefresh() {
        episodeRefreshRequestId++
        episodeRefreshJob?.cancel()
        episodeRefreshJob = null
    }

    /** 每次打开选集都强制回源；只更新当前线路的选集信息，不动播放和线路焦点。 */
    private fun refreshEpisodesForPanel() {
        if (_ui.value.isLive) return
        val item = currentItem ?: return
        if (item.detailRef.isBlank()) return
        invalidateEpisodeRefresh()
        val state = _ui.value
        val line = state.lines.getOrNull(state.activeLine)
        val ticket = EpisodeRefreshTicket(
            requestId = episodeRefreshRequestId,
            vodId = item.id,
            detailRef = item.detailRef,
            playbackGeneration = sessionGen,
            episodeIndex = state.epIndex,
            activeLine = state.activeLine,
            sourceIdentity = repo.episodeSourceIdentity(line),
        )
        episodeRefreshJob = viewModelScope.launch {
            try {
                val fresh = try {
                    repo.resolveDetail(ticket.vodId, forceRefresh = true)
                } catch (cancelled: kotlinx.coroutines.CancellationException) {
                    throw cancelled
                } catch (_: Exception) {
                    null
                } ?: return@launch
                val latest = _ui.value
                val latestItem = currentItem ?: return@launch
                val latestLine = latest.lines.getOrNull(latest.activeLine)
                val latestSourceIdentity = repo.episodeSourceIdentity(latestLine)
                if (!EpisodeRefreshPolicy.isCurrentRequest(
                        request = ticket,
                        latestRequestId = episodeRefreshRequestId,
                        vodId = latestItem.id,
                        detailRef = latestItem.detailRef,
                        playbackGeneration = sessionGen,
                        episodeIndex = latest.epIndex,
                        activeLine = latest.activeLine,
                        sourceIdentity = latestSourceIdentity,
                    )) return@launch
                val incomingEpisodes = repo.episodesForLine(fresh, latestLine)
                    ?: if (latestLine?.playRef.isNullOrBlank()) fresh.episodes else null
                if (incomingEpisodes.isNullOrEmpty()) return@launch
                val incomingIdentity = latestSourceIdentity ?: repo.defaultEpisodeSourceIdentity(fresh)
                val currentNames = latest.epNames.ifEmpty { latestItem.episodes.map { it.name } }
                if (!EpisodeRefreshPolicy.accepts(
                        currentNames = currentNames,
                        incomingNames = incomingEpisodes.map { it.name },
                        currentLines = latest.lines.size,
                        incomingLines = latest.lines.size,
                        keepIndex = latest.epIndex,
                        sameEpisodeSource = episodeNamesSourceIdentity == incomingIdentity,
                    )) return@launch
                currentItem = fresh.copy(episodes = incomingEpisodes)
                episodeNamesSourceIdentity = incomingIdentity
                _ui.value = latest.copy(
                    epCount = incomingEpisodes.size,
                    epNames = incomingEpisodes.map { it.name },
                )
            } finally {
                if (ticket.requestId == episodeRefreshRequestId) episodeRefreshJob = null
            }
        }
    }

    /**
     * 选集直切（v1.19）：更多面板选集不再走路由新建 VM——旧版连续跳 10 集会在返回栈
     * 堆 10 个 PlayerViewModel（每个带完整引擎实例/LoadControl 缓冲/看门狗协程全部
     * 驻留），低端盒内存压力大；且逐级 BACK 会逐集自动恢复播放上一集。
     * 同一 VM 内换集：懒解析源的 playRef 按集生成（vodKey/-e{n} 参数），须重解析线路表。
     */
    fun switchEpisode(ep: Int) {
        val cur = _ui.value
        if (!hasEngine() || ep !in 0 until cur.epCount || ep == cur.epIndex || cur.isLive) return
        val request = beginManualPlaybackRequest()
        if (hasEngine()) runCatching { engine.pause() }
        stallRecoveries = 0
        triedLines.clear()
        recoveryBudget.reset()
        healthCooldown.reset()
        _ui.value = cur.copy(
            epIndex = ep,
            lines = emptyList(),
            fatalMsg = null,
            sourceStatus = PlayerSourceStatus.LOADING,
            playState = EnginePlayState.BUFFERING,
        )
        val requestGeneration = sessionGen
        val requestLine = cur.activeLine
        val requestEngine = engine
        val requestEngineEpoch = engineEpochCounter
        val job = viewModelScope.launch {
            saveProgress(force = true, snapshot = cur)
            if (!isCurrentManualPlaybackRequest(
                    request, requestGeneration, requestLine, requestEngine, requestEngineEpoch,
                )) return@launch
            val item = currentItem ?: return@launch
            val resolvedLines = try {
                repo.resolveLines(item, ep)
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (failure: Exception) {
                OtvLog.w("换集解析失败 ep=$ep: ${failure.message}")
                emptyList()
            }
            if (!isCurrentManualPlaybackRequest(
                    request, requestGeneration, requestLine, requestEngine, requestEngineEpoch,
                )) return@launch
            val newLines = VisibleVodLinePolicy.apply(resolvedLines)
            if (newLines.isEmpty()) {
                stopForFatal()
                _ui.value = _ui.value.copy(
                    lines = emptyList(),
                    fatalMsg = "这一集暂时没有可用片源，请重试或切换线路",
                    sourceStatus = PlayerSourceStatus.EMPTY,
                    playState = EnginePlayState.IDLE,
                )
                return@launch
            }
            repo.acceptPlayRefSnapshots(newLines)
            _ui.value = _ui.value.copy(
                epCount = item.episodes.size.coerceAtLeast(1),
                epNames = item.episodes.map { it.name },
                lines = newLines,
                sourceStatus = PlayerSourceStatus.LOADING,
                playState = EnginePlayState.IDLE,
            )
            val line = requestLine.coerceIn(0, (newLines.size - 1).coerceAtLeast(0))
            // 换集后重建解码实例，清掉上一集留下的播放器/渲染状态。
            startSession(line, 0L, recreateEngine = true)
        }
        manualSessionJob = job
    }

    fun requestExit() {
        _ui.value = _ui.value.copy(showExitDialog = true)
    }

    fun dismissExit() {
        _ui.value = _ui.value.copy(showExitDialog = false)
    }

    /** 任意交互刷新控制条自动隐藏计时 */
    fun touch() {
        // 由 Screen 的 LaunchedEffect(ui.controlsVisible, tick) 处理；这里仅暴露信号
    }

    fun onHostPause() {
        if (hostPausedAtMs == 0L) hostPausedAtMs = android.os.SystemClock.elapsedRealtime()
        if (_ui.value.isPlaying) {
            systemResumeNeeded = true
            engine.pause()
        }
        viewModelScope.launch { saveProgress(force = true) }
    }

    fun onHostResume() {
        val decision = com.qiubo.optimaltv.lifecycle.AppLifecyclePolicy.onForeground(
            backgroundedAtMs = hostPausedAtMs,
            nowMs = android.os.SystemClock.elapsedRealtime(),
            isLive = _ui.value.isLive,
        )
        hostPausedAtMs = 0L
        if (systemResumeNeeded && !userPaused) {
            engine.play()
        }
        systemResumeNeeded = false
        when (decision) {
            com.qiubo.optimaltv.lifecycle.ForegroundDecision.REFRESH_AND_RENEW_LIVE ->
                recoverPlaybackStall("后台超过 30 秒，刷新直播地址")
            com.qiubo.optimaltv.lifecycle.ForegroundDecision.REFRESH ->
                if (_ui.value.isLive) renewKick.trySend(Unit)
            com.qiubo.optimaltv.lifecycle.ForegroundDecision.NONE -> Unit
        }
    }

    override fun onCleared() {
        // 立即落盘当前进度（同步快照，防丢）
        val s = _ui.value
        val item = currentItem
        if (item != null && !isFootballVod && s.durationMs > 0 && s.positionMs > 1_000) {
            // v1.19 流畅度：旧版 runBlocking 在主线程干等 Room executor 往返，返回列表的
            // 转场动画被卡；onCleared 后 viewModelScope 即将取消，改挂 app 生命周期 scope
            val progress = ProgressEntity(
                vodKey = item.vodKey(s.epIndex),
                vodId = item.id,
                epIndex = s.epIndex,
                positionMs = s.positionMs,
                durationMs = s.durationMs,
                updatedAt = System.currentTimeMillis(),
            )
            val history = HistoryEntity(
                vodId = item.id,
                title = item.title,
                posterUrl = item.posterUrl,
                epIndex = s.epIndex,
                positionMs = s.positionMs,
                durationMs = s.durationMs,
                updatedAt = System.currentTimeMillis(),
            )
            Graph.appScope.launch {
                runCatching {
                    db.vodDao().upsertProgress(progress)
                    db.vodDao().upsertHistory(history)
                }
            }
        }
        if (com.qiubo.optimaltv.lifecycle.ExitCoordinator.isExiting()) {
            // ExitCoordinator terminates this process shortly after Activity teardown. Avoid
            // blocking its main-thread finish path on ExoPlayer's synchronous native release.
            runCatching { if (hasEngine()) engine.pause() }
        } else {
            runCatching { if (hasEngine()) engine.release() }
        }
        renewJob?.cancel()
        liveAutoRetryJob?.cancel()
        // v1.21：退出播放页即停投屏（渲染器 Stop；不回本地——页面已销毁）
        castPollJob?.cancel()
        _ui.value.castDevice?.let { dev ->
            Thread { runCatching { kotlinx.coroutines.runBlocking { DlnaControl.stop(dev) } } }.start()
        }
        super.onCleared()
    }
}

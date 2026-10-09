package com.qiubo.optimaltv.ui.player

import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.OtvLog
import com.qiubo.optimaltv.data.db.HistoryEntity
import com.qiubo.optimaltv.data.db.ProgressEntity
import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.data.prefs.AspectMode
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.data.repo.SelectedSourceRefreshException
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
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

enum class Panel { NONE, MORE }

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
    ): Boolean {
        if (keepIndex !in 0 until incomingNames.size) return false
        if (incomingNames.size < currentNames.size) return false
        if (incomingNames.size > currentNames.size) return true
        if (incomingLines > currentLines) return true
        // 同集数但集名/线路内容变了：仍然替换，否则「更新了但不显示」。
        return incomingNames != currentNames
    }
}

/** Manual selection of the active VOD line is an explicit retry; live picker behavior stays unchanged. */
internal object ManualLineSelectionPolicy {
    fun shouldStart(lineCount: Int, activeLine: Int, selectedLine: Int, isLive: Boolean): Boolean =
        selectedLine in 0 until lineCount && (!isLive || selectedLine != activeLine)
}

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
    /** Current line/detail resolution is in progress; independent of engine buffering state. */
    val sourceResolving: Boolean = false,
    val engineId: String = "media3",
    val engineName: String = "",
    // 引擎状态镜像（驱动 UI）
    val playState: EnginePlayState = EnginePlayState.IDLE,
    val isPlaying: Boolean = false,
    val positionMs: Long = 0,
    val durationMs: Long = 0,
    val speed: Float = 1f,
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
    /* 需求⑭（2026-09-08）+ 2026-09-11 追加：TV 版投屏全删——v1.21 DLNA 状态字段随共享层
       一并剥离（移动版 PlayerViewModel 保留完整 DLNA；sync_shared 差异允许清单有备案） */
)

/**
 * 播放会话协调器（技术方案 §3.1 业务层）：
 * 起播看门狗 20s / 卡顿中断检测 / 失败自动换线 / 线路记忆 / 断点续播。
 * 进度保存按 5s 节流（低配机 localStorage 同步教训移植）。
 */
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

    private val _ui = MutableStateFlow(PlayerUiState(epIndex = epStart))
    val ui: StateFlow<PlayerUiState> = _ui.asStateFlow()

    lateinit var engine: MediaEngine
        private set

    fun hasEngine(): Boolean = this::engine.isInitialized

    private var startedThisSession = false
    /** 会话代数：并发的 startSession 只允许最新一代操作引擎（防旧源倒灌） */
    private var sessionGen = 0
    /** 当前正在解析点播地址的起播任务；手动切源/换集会立即取消并作废旧结果。 */
    private var pendingVodSessionJob: Job? = null
    /** 防止同一引擎会话重复收到 ENDED 时重复切集。 */
    private var handledEndedSessionGen = -1
    private var stallRecoveries = 0
    /** Unified recovery ladder step; reset only after the new session reaches READY. */
    private var recoveryAttempt = 0
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
    /**
     * 本次起播的埋点会话（2026-09-28）。点播放时开一次，量到首帧后结算出
     * detail/resolve/prepare/first_frame/total 五个耗时。跨换线沿用同一个会话，
     * 因为用户视角的「点播放 → 出画面」就是一次，不该被内部换线切成多次。
     */
    private var telemetry: PlaybackTelemetry.Session = PlaybackTelemetry.newSession()
    private var progressJob: Job? = null
    private var episodeRefreshJob: Job? = null
    /** 直播静默续签循环（90s 重新解析当前源 → 签名变化即热切换） */
    private var renewJob: Job? = null
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

    /** 直播：与 lines 平行的信号源标识（bb/plu/qqlive100..），续签时只重解析当前源 */
    private var liveSrcs: List<String> = emptyList()
    private var liveMatchId: String? = null
    /** 引擎实际在播的直播地址（v1.14 续签对比基准；线路表只作展示/手动换线入口） */
    private var livePlayingUrl: String? = null

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
     * 旧版渲染层按 engineId 取 key，而同类型重建（卡顿阶梯 RECREATE_ENGINE、直播
     * 30s 自动重试、错误态手动重试）时 engineId 不变 → 视图树继续挂着**已 release**
     * 的旧 PlayerView，新引擎的渲染面从未挂进窗口 ⇒ 整屏全黑、声音照常；换源/重试
     * 都救不回画面（新声音进新引擎，画面进不来）。
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

    /**
     * 进错误兜底态前先停播（2026-09-21）：错误卡是「无法播放」语义，旧版只把画面遮住、
     * 不停引擎，实测「卡片已弹出、背景音照常响」。重试会重新 prepare，不需要保留播放态。
     */
    private fun stopForFatal() {
        runCatching { if (hasEngine()) engine.pause() }
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
    private var sourceBootJob: Job? = null

    /** 足球回放/集锦（replay:/hhkanplay:）不进观看历史/进度库——与直播同规则，
     *  否则比赛录像以「埃弗顿 对阵 曼联 第1集」混进影视最近观看（用户反馈）。 */
    private val isFootballVod = vodId.startsWith("replay:") || vodId.startsWith("hhkanplay:")

    init {
        if (autoBoot) sourceBootJob = viewModelScope.launch { boot() }
    }

    private fun launchSourceBoot(fresh: Boolean) {
        sourceBootJob?.cancel()
        _ui.value = _ui.value.copy(sourceResolving = true, fatalMsg = null)
        sourceBootJob = viewModelScope.launch { boot(fresh = fresh) }
    }

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

    /** 直播分支（vodId = "live:{matchId}"）：6 信号源并发解析 → relay 包裹，复用换线/看门狗。
     *  fresh=true（失败重试链路）：后端绕过 300s 成功缓存强制重新解析拿新签名。
     *  默认起播源（2026-09-07 需求③）：上次手动选择的信号源（liveLastSrc 持久化）；
     *  无记忆或记忆源不在本场线路表（qqlive 编号随场次轮换）→ 回退 bb（原版足球直播2）。
     *  v1.10（2026-08-31 低端机起播提速）：旧版「全 6 源并发 awaitAll → startSession 再对
     *  bb fresh 重解析」= 双重解析，最慢源拖住起播。现在：首选源单源先解析（享受后端 300s
     *  成功缓存）→ 立即复用该 URL 起播；其余源后台到货即上屏（需求②：不再等全源到齐）。
     *  首选源不可用才回退全源并发链路。标题异步补（不阻塞起播）。 */
    private suspend fun bootLive(fresh: Boolean = false) {
        val matchId = vodId.removePrefix("live:")
        liveMatchId = matchId
        // 标题异步补：matches() 冷抓可能 20s+，绝不能挡在起播链路上（LiveScreen 进入过则为缓存命中）
        viewModelScope.launch {
            val m = runCatching { Graph.live.matches().find { it.matchId == matchId } }.getOrNull()
            if (m != null) _ui.value = _ui.value.copy(title = "${m.home} 对阵 ${m.away}")
        }
        // 需求③：首选源 = 上次手动选择的源；记忆源不在本场动态线路表时回退 bb
        val srcTable = Graph.live.srcsFor(matchId)
        val remembered = runCatching { Graph.settings.liveLastSrc() }.getOrNull().orEmpty()
        val preferred = if (remembered.isNotBlank() && srcTable.any { it.first == remembered }) remembered else "bb"
        if (preferred != "bb") OtvLog.i("bootLive：需求③ 直选上次信号源 $preferred（bb 兜底）")
        // 快路径：仅解析首选源——冷启动也只做 1 次频道页+播放器页+解密，
        // 而非旧版的全源并发（低端机 WebView 解密串行，最慢源拖垮整体）。
        // 显示名从 srcsFor（站点 channels 原始命名）取，与慢路径/选择条补齐一致
        val prefLabel = srcTable.firstOrNull { it.first == preferred }?.second
            ?: com.qiubo.optimaltv.data.repo.LiveRepository.srcLabel("bb")
        val tBb = android.os.SystemClock.elapsedRealtime()
        val prefUrl = if (!fresh) runCatching { Graph.live.streamUrlForSrc(matchId, preferred, fresh = false) }.getOrNull() else null
        OtvLog.i("boot 计时：$preferred 解析 ${android.os.SystemClock.elapsedRealtime() - tBb}ms（命中=${prefUrl != null}）")
        if (!prefUrl.isNullOrBlank()) {
            OtvLog.i("bootLive fast-path：$preferred（$prefLabel）单源解析成功 → 立即起播，其余源后台补齐")
            if (this::engine.isInitialized) runCatching { engine.release() }
            installEngine(EngineRegistry.create(settings.current().engineId, Graph.appContext))
            liveSrcs = listOf(preferred)
            _ui.value = _ui.value.copy(
                title = "足球直播", epCount = 1,
                lines = listOf(LineInfo(vodId, prefLabel, prefUrl)),
                isLive = true,
                engineId = engine.engineId, engineName = engine.displayName,
                aspectOrdinal = settings.current().aspectOrdinal,
            )
            collectEngineState()
            startLiveRenewal()
            startSession(0, 0L, reuseResolvedUrl = true)
            appendRemainingLiveSrcs(matchId, fresh, skipSrc = preferred)
            return
        }
        val pairs = runCatching { Graph.live.streamUrlPairs(matchId, fresh) }.getOrDefault(emptyList())
        OtvLog.i("bootLive match=$matchId fresh=$fresh 信号源=${pairs.size} 个")
        if (pairs.isEmpty()) {
            OtvLog.w("bootLive 无可用信号 → fatalMsg")
            stopForFatal()
            _ui.value = _ui.value.copy(fatalMsg = "直播信号暂不可用", title = "足球直播", isLive = true)
            scheduleLiveAutoRetry()
            return
        }
        liveSrcs = pairs.map { it.first }
        val lines = pairs.mapIndexed { _, p ->
            LineInfo(vodId, p.third, p.second)
        }
        // 重试路径重建：先释放旧引擎（否则每次重试泄漏一个 ExoPlayer 实例）
        if (this::engine.isInitialized) runCatching { engine.release() }
        // 调试注入的引擎强制（am start --es otvEngine vlc，测试软解路径用；一次性消费）
        val engineOverride = com.qiubo.optimaltv.DebugNavBus.pendingEngine?.also {
            com.qiubo.optimaltv.DebugNavBus.pendingEngine = null
        }
        installEngine(EngineRegistry.create(engineOverride ?: settings.current().engineId, Graph.appContext))
        _ui.value = _ui.value.copy(
            title = "足球直播", epCount = 1, lines = lines, isLive = true,
            engineId = engine.engineId, engineName = engine.displayName,
            aspectOrdinal = settings.current().aspectOrdinal,
        )
        collectEngineState()
        startLiveRenewal()
        // 需求③：首选源=上次选择（在场则直选）；缺 bb/记忆源时回退首个可用源
        //（错误态自动重试会再回首选源）
        val selIdx = liveSrcs.indexOf(preferred).takeIf { it >= 0 } ?: 0
        if (selIdx > 0) {
            OtvLog.i("bootLive：首选源 $preferred 位于第 ${selIdx + 1} 位，直选之")
        } else if (liveSrcs.firstOrNull() != "bb") {
            OtvLog.w("bootLive：bb/记忆源（$preferred）未解析出，回退 ${liveSrcs.firstOrNull()}")
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

    /** 快路径起播后，其余信号源后台补齐进选择条。
     *  2026-09-07 需求②：改用 streamUrlsIncremental（每源到货即上屏）——旧版
     *  streamUrlPairs awaitAll 等全源到齐才一次性上屏，串行解密桥下大场次要等
     *  分钟级才有完整选择条（用户报障「只有一个信号源，几分钟才出现所有源」）。
     *  v1.23：源表来自 /api/matches 动态 channels（qqlive 编号随站点轮换），显示名用
     *  站点原始命名（「高清足球直播24」「CCTV5+」），硬编码映射仅作 label 兜底。
     *  lines 与 liveSrcs 必须同步追加（两表按下标平行，续签按 liveSrcs[activeLine] 取源）。 */
    private fun appendRemainingLiveSrcs(matchId: String, fresh: Boolean, skipSrc: String = "bb") {
        viewModelScope.launch {
            runCatching {
                Graph.live.streamUrlsIncremental(matchId, fresh) { (src, url, label) ->
                    val s = _ui.value
                    if (src == skipSrc || liveSrcs.contains(src)) return@streamUrlsIncremental
                    _ui.value = s.copy(lines = s.lines + LineInfo(vodId, label, url))
                    liveSrcs = liveSrcs + src
                }
            }
        }
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
                val mid = liveMatchId ?: break
                val idx = _ui.value.activeLine
                val src = liveSrcs.getOrNull(idx) ?: continue
                val s = _ui.value
                if (!s.isLive || s.fatalMsg != null) continue
                if (s.playState != EnginePlayState.READY && s.playState != EnginePlayState.BUFFERING) continue
                val playingUrl = livePlayingUrl
                if (playingUrl.isNullOrBlank()) continue
                if (s.playState == EnginePlayState.BUFFERING) {
                    val fresh = runCatching { Graph.live.streamUrlForSrc(mid, src, fresh = true) }.getOrNull()
                    if (!fresh.isNullOrBlank()) {
                        writeBackLineUrl(idx, fresh)
                        livePlayingUrl = fresh
                        OtvLog.w("live renew：BUFFERING → fresh 同源换地址")
                        engine.swapSource(fresh)
                    }
                    continue
                }
                if (!s.isPlaying) {
                    // 暂停中：先廉价健康检查（不打上游解析）；活着就什么都不做
                    val alive = runCatching { Graph.live.playlistAlive(playingUrl) }.getOrDefault(true)
                    if (alive) continue
                    val fresh = runCatching { Graph.live.streamUrlForSrc(mid, src, fresh = true) }.getOrNull()
                    if (fresh.isNullOrBlank() || fresh == playingUrl) continue
                    writeBackLineUrl(idx, fresh)
                    livePlayingUrl = fresh
                    if (hasEngine() && engine.engineId == "media3") {
                        OtvLog.i("live renew：暂停中旧地址已死 → 静默热切换（保持暂停）")
                        engine.swapSource(fresh, autoplay = false)
                    } else {
                        // VLC 重开必自动播放，不静默换；恢复播放停滞由卡顿阶梯接手（会拿到新地址）
                        OtvLog.i("live renew：暂停中旧地址已死（VLC 引擎，恢复时由阶梯重开新地址）")
                    }
                    continue
                }
                val freshUrl = runCatching { Graph.live.streamUrlForSrc(mid, src, fresh = true) }.getOrNull()
                if (freshUrl.isNullOrBlank()) {
                    OtvLog.w("live renew：src=$src 解析失败（继续播当前流，下轮再试）")
                    continue
                }
                if (freshUrl == playingUrl) continue   // 签名未变化，无需动作
                // v1.14 健康延后：旧地址仍在服务期 → 不切（消除无意义冻结）
                val oldAlive = runCatching { Graph.live.playlistAlive(playingUrl) }.getOrDefault(true)
                if (oldAlive) {
                    OtvLog.i("live renew：签名已变但旧地址仍在服务 → 本轮不切（消除冻结）")
                    continue
                }
                OtvLog.i("live renew：旧地址已死 → 热切换 line=$idx src=$src（buf=${_ui.value.bufferedAheadMs}ms）")
                writeBackLineUrl(idx, freshUrl)
                engine.swapSource(freshUrl)
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
    private fun collectEngineState() {
        engineStateJob?.cancel()
        engineStateJob = viewModelScope.launch {
            engine.uiState.collect { st ->
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
                when {
                    st.state == EnginePlayState.ENDED -> handlePlaybackEnded()
                    st.state == EnginePlayState.READY && !startedThisSession -> onSessionStarted(st.positionMs)
                    st.state == EnginePlayState.ERROR -> {
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
        // v1.14：冷启动秒进播放器时先等内置后端就绪（最多 ~15s）——backend 未监听时
        // 详情请求连接拒绝被 runCatching 吞掉，直接假性「该集没有可用线路」
        val tBoot = android.os.SystemClock.elapsedRealtime()
        runCatching { repo.awaitBackendReady() }
        OtvLog.i("boot 计时：awaitBackendReady ${android.os.SystemClock.elapsedRealtime() - tBoot}ms")
        when {
            // 直播比赛走独立分支（无目录依赖）
            vodId.startsWith("live:") -> { bootLive(fresh); return }
            // IPTV 电视直播（v1.13）：公开 m3u 源原生直连，无目录依赖
            vodId.startsWith("iptv:") -> { bootIptv(); return }
            // 回放（原版 playRewatch：hhkan 搜索 → 多线路）
            vodId.startsWith("replay:") -> { bootReplay(); return }
            // 集锦/已知 hhkan vid 直入播放器（原版 playVodDirect）
            vodId.startsWith("hhkanplay:") -> { bootHhkanVid(); return }
        }
        _ui.value = _ui.value.copy(sourceResolving = true, fatalMsg = null)
        // 等目录就绪（首页已 refresh，一般瞬时）
        var guard = 0
        while (repo.state.value.catalog == null && guard++ < 100) delay(100)
        val item = try {
            repo.itemById(vodId)
                ?: repo.resolveDetail(vodId, forceRefresh = fresh)   // 目录外条目（实时搜索结果）兜底
        } catch (cancelled: kotlinx.coroutines.CancellationException) {
            throw cancelled
        } catch (failure: Exception) {
            currentCoroutineContext().ensureActive()
            Log.w(TAG, "影片详情加载失败 id=$vodId: ${failure.message}")
            _ui.value = _ui.value.copy(sourceResolving = false, fatalMsg = "影片详情加载失败，请重试")
            return
        }
        currentCoroutineContext().ensureActive()
        if (item == null) {
            stopForFatal()
            _ui.value = _ui.value.copy(sourceResolving = false, fatalMsg = "影片不存在或源未加载")
            return
        }
        // 懒解析源（hhkan）：先补全选集元数据，vodKey/epCount 才正确
        var effItem = try {
            repo.resolveDetail(item.id, forceRefresh = fresh) ?: item
        } catch (cancelled: kotlinx.coroutines.CancellationException) {
            throw cancelled
        } catch (failure: Exception) {
            currentCoroutineContext().ensureActive()
            Log.w(TAG, "片源详情刷新失败 id=${item.id}: ${failure.message}")
            _ui.value = _ui.value.copy(sourceResolving = false, fatalMsg = "片源刷新失败，请重试")
            return
        }
        currentCoroutineContext().ensureActive()
        currentItem = effItem
        // 埋点：解析开始（详情与片源解析并行的起点就在这里）。
        telemetry.mark(
            PlaybackTelemetry.Stage.RESOLVE_START,
            source = effItem.detailRef,
            detail = "title=${effItem.title}",
        )
        // 线路构建：走 L0→L5 取源阶梯（last_good → 云端 resolver → 本地全量匹配 →
        // 云端备用 → 跨源同名），拿到的首选项直接排到第 0 位起播，不等全部线路到齐。
        var pick = try {
            repo.pickPlayableSources(effItem, epStart, db.lastGoodSourceDao())
        } catch (cancelled: kotlinx.coroutines.CancellationException) {
            throw cancelled
        } catch (failure: Exception) {
            currentCoroutineContext().ensureActive()
            Log.w(TAG, "片源解析失败 id=${effItem.id}: ${failure.message}")
            _ui.value = _ui.value.copy(sourceResolving = false, fatalMsg = "片源解析失败，请重试")
            return
        }
        currentCoroutineContext().ensureActive()
        // A cached quick detail can omit slower HHKan source matches. If it produced no
        // playable lines, refresh the detail once with the source-enrichment path before
        // declaring the title empty; successful first-pass playback stays fast.
        if (pick.isEmpty && !fresh && effItem.detailRef.isNotBlank()) {
            val enrichedItem = try {
                repo.resolveDetail(effItem.id, forceRefresh = true) ?: effItem
            } catch (cancelled: kotlinx.coroutines.CancellationException) {
                throw cancelled
            } catch (failure: Exception) {
                currentCoroutineContext().ensureActive()
                Log.w(TAG, "空线路后的补充匹配失败 id=${effItem.id}: ${failure.message}")
                effItem
            }
            currentCoroutineContext().ensureActive()
            effItem = enrichedItem
            currentItem = enrichedItem
            pick = try {
                repo.pickPlayableSources(enrichedItem, epStart, db.lastGoodSourceDao())
            } catch (cancelled: kotlinx.coroutines.CancellationException) {
                throw cancelled
            } catch (failure: Exception) {
                currentCoroutineContext().ensureActive()
                Log.w(TAG, "补充匹配后的线路解析失败 id=${effItem.id}: ${failure.message}")
                pick
            }
            currentCoroutineContext().ensureActive()
        }
        if (!pick.isEmpty) {
            telemetry.mark(
                PlaybackTelemetry.Stage.RESOLVE_FIRST_SOURCE,
                source = pick.lines.getOrNull(pick.preferredIndex)?.playRef.orEmpty(),
                detail = "tier=${pick.tier.wire}",
            )
        }
        val lines = VisibleVodLinePolicy.apply(pick.lines)
        if (lines.isEmpty()) {
            stopForFatal()
            telemetry.mark(
                PlaybackTelemetry.Stage.FATAL,
                errorClass = PlaybackTelemetry.classify("no available line"),
                detail = "该集没有可用线路",
            )
            _ui.value = _ui.value.copy(
                sourceResolving = false,
                fatalMsg = "该集没有可用线路",
                title = effItem.title,
            )
            return
        }

        // 线路记忆直选（方案 §3.2-3），否则用设置里的引擎偏好
        val memory = settings.lastLine(vodId)
        val enginePref = memory?.first ?: settings.current().engineId
        // 阶梯选中的线路优先于历史记忆——last_good/resolver 刚给出的那条是「当前最可能
        // 能播」的，历史记忆可能已经失效（签名轮换、线路下线）。
        val initialLine = pick.preferredIndex.takeIf { it in lines.indices && pick.hasImmediate }
            ?: memory?.second?.takeIf { it in lines.indices }
            ?: 0

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
                    val versionish = effItem.episodes.size in 2..4 &&
                        effItem.episodes.none {
                            it.name.contains("集") || it.name.contains("期") || it.name.contains("话")
                        }
                    com.qiubo.optimaltv.ui.search.classifyVod(
                        effItem.meta,
                        if (versionish) 1 else effItem.episodes.size,
                    ) == "电影"
                }
            }
        }
        _ui.value = _ui.value.copy(
            title = effItem.title,
            epCount = effItem.episodes.size,
            epNames = effItem.episodes.map { it.name },
            isMovie = isMovie,
            meta = effItem.meta.ifBlank {
                (listOf(effItem.year, effItem.area).filter { it.isNotBlank() } + effItem.tags).joinToString(" / ")
            },
            desc = effItem.desc,
            posterUrl = effItem.posterUrl,
            lines = lines,
            sourceResolving = false,
            engineId = engine.engineId,
            engineName = engine.displayName,
            aspectOrdinal = settings.current().aspectOrdinal,
        )

        // 收集引擎状态镜像（与直播分支共用）
        collectEngineState()

        // 续播位置（方案 §3.4 断点续播；t<1s 不算有效进度）：
        // v1.19 优先用路由带入的跨线路位置（详情页扫全部线路取最近观看）；
        // 无带入（最近观看卡直达/调试注入）走本线路 DB 查询
        val key = effItem.vodKey(epStart)
        val saved = db.vodDao().getProgress(key)
        val dbPos =
            if (saved != null && saved.positionMs > 1_000 &&
                (saved.durationMs <= 0 || saved.positionMs < saved.durationMs - 30_000)
            ) saved.positionMs else 0L
        val startPos = if (resumeStartMs > 1_000) resumeStartMs else dbPos
        if (startPos > 0) Log.i(TAG, "resume $key @${startPos}ms (route=${resumeStartMs > 0})")

        startSession(initialLine, startPos)
    }

    private suspend fun startSession(
        lineIdx: Int,
        startPos: Long,
        reuseResolvedUrl: Boolean = false,
        forceFresh: Boolean = false,
    ) {
        // 会话代数（v1.14）：看门狗换线、直连重试、卡顿重解析可能并发触发 startSession，
        // 各自挂起在解析上——旧会话恢复后必须发现自己已被取代并放弃，否则会把引擎
        // 倒灌回旧源（实测：换线 line1 已起播，8s 前的直连重试落地把引擎拽回慢速 line0）
        val thisJob = currentCoroutineContext()[Job]
        if (!_ui.value.isLive && thisJob != null) {
            pendingVodSessionJob?.takeIf { it !== thisJob && it.isActive }?.cancel()
            pendingVodSessionJob = thisJob
        }
        val gen = ++sessionGen
        startedThisSession = false
        stallRecoveries = 0
        triedLines += lineIdx
        _ui.value = _ui.value.copy(
            activeLine = lineIdx,
            fatalMsg = null,
            sourceResolving = !_ui.value.isLive,
        )
        // 兜底：线路表为空/索引越界时不抛异常（否则协程未捕获直接闪退），走防线三错误态
        val line = _ui.value.lines.getOrNull(lineIdx) ?: run {
            _ui.value = _ui.value.copy(sourceResolving = false)
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
                val mId = liveMatchId!!
                url = runCatching { Graph.live.streamUrlForSrc(mId, src, fresh = true) }.getOrNull()
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
        val playUrl = try {
            url ?: run {
                // v1.14 点播默认走本地中继（多连接并行拉片对抗 CDN 单连接限速，根治
                // 「20 分钟后频繁卡顿」）；该线路中继已被判不可用时直连
                val preferRelay = line.playRef.isBlank() || line.playRef !in relayBroken
                repo.resolvePlayUrl(line, preferRelay, forceRefresh = forceFresh)
            }
        } catch (cancelled: kotlinx.coroutines.CancellationException) {
            throw cancelled
        } catch (unavailable: SelectedSourceRefreshException) {
            currentCoroutineContext().ensureActive()
            if (gen != sessionGen) return
            stopForFatal()
            _ui.value = _ui.value.copy(
                sourceResolving = false,
                fatalMsg = unavailable.message ?: "无法刷新所选信号源，请重试",
            )
            return
        }
        currentCoroutineContext().ensureActive()
        if (gen != sessionGen) {
            OtvLog.w("line $lineIdx 起播准备期间已有更新会话（gen 落后），放弃旧源")
            return
        }
        _ui.value = _ui.value.copy(sourceResolving = false)
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
        // VLC 兜底引擎（v1.8）：与 Media3 同走 /api/relay HLS 中继——真实故障流
        // （aarray High 压段）实测该路径 9 分 10 秒零卡顿；后端另有 /api/relayts
        // 连续 TS 端点（实验设施，VLC ts demux 对拼接段时间戳重置容忍差，弃用）
        Log.i(TAG, "session start line=$lineIdx engine=${engine.engineId} url=$playUrl")
        OtvLog.i("startSession line=$lineIdx engine=${engine.engineId} startPos=$startPos url=${playUrl.take(120)}")
        // 埋点：拿到直链、交引擎前的这一刻。只记线路哈希与耗时，不记地址本身。
        telemetry.mark(
            PlaybackTelemetry.Stage.PLAYER_PREPARE,
            source = playUrl,
            detail = "engine=${engine.engineId} line=$lineIdx",
        )
        engine.prepare(PrepareRequest(playUrl, startPos))
        armWatchdog()
        armFirstFrameWatchdog()
        startStallMonitor()
        startHealthMonitor()
        prefetchLines()
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
    /**
     * 点播首帧分级阶梯的协程句柄（2026-09-28）。
     *
     * 之前 [armVodFirstFrameLadder] 直接 `viewModelScope.launch` 却不持有 Job，
     * 换线时旧阶梯不会被取消，会切走刚起播的新线路。这里显式持有，换线时先 cancel。
     */
    private var vodLadderJob: Job? = null
    /** 阶梯代次跟踪：作废旧代阶梯、保证「只升一档」。 */
    private val firstFrameLadder = FirstFrameRecoveryLadderTracker()
    /**
     * 上次**自动**切线时刻（单调时钟，-1 = 从未切过）。
     * 供 [LongPlaybackHealthPolicy.canSwitchNow] 做 20–30s 切线冷却：
     * 刚失败/刚切过的线在冷却内不自动切回，防止「线1→线2→线1」把线路耗光。
     * 用户手动切线（[switchLine]）不算自动切线，不受冷却约束。
     */
    private var lastAutoSwitchAt = -1L

    private fun armFirstFrameWatchdog() {
        firstFrameJob?.cancel()
        armVodFirstFrameLadder()
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

    /**
     * 点播首帧分级看门狗（2026-09-28）。
     *
     * 旧的 [armFirstFrameWatchdog] 只管直播且只有一个 15s 硬超时；点播根本没有首帧
     * 看门狗（注释里写「有进度语义，黑屏可由用户 seek 感知」），于是点播黑屏只能等
     * 20s 起播看门狗兜底——用户盯着 20s 黑屏。
     *
     * 这里按 [FirstFrameRecoveryPolicy] 的四档渐进处置（3s 预备 / 5s 切 / 15s 硬超时），
     * 每档只做一次、只升不降，避免同一起播反复折腾。
     */
    private fun armVodFirstFrameLadder() {
        if (_ui.value.isLive) return
        // 换线必须先取消上一代阶梯：否则旧协程仍按自己的计时继续跑，会在新线路刚起播时
        // 就判定「已等 7s」并把它切走——用户看到的是「刚切成功又被切走、反复横跳」。
        vodLadderJob?.cancel()
        val startedAt = android.os.SystemClock.elapsedRealtime()
        val generation = firstFrameLadder.beginGeneration()
        vodLadderJob = viewModelScope.launch {
            while (isActive && !_ui.value.isLive) {
                delay(500)
                // 代次校验：换线后旧代一律停止。即便本协程侥幸没被取消（正在执行动作体的
                // 那个窗口），代次也不匹配，动作不会生效。
                if (!firstFrameLadder.isCurrent(generation)) return@launch
                val s = _ui.value
                if (s.fatalMsg != null) return@launch
                // 已经出过帧（引擎报 firstFrame）或进入 READY 且位置在动 → 本次起播完成。
                val rendered = if (hasEngine()) engine.uiState.value.firstFrameRendered else false
                if (rendered || (s.playState == EnginePlayState.READY && s.positionMs > 0)) {
                    firstFrameLadder.markFirstFrame(generation)
                    return@launch
                }
                val elapsed = android.os.SystemClock.elapsedRealtime() - startedAt
                val onMedia3 = hasEngine() && engine.engineId == "media3"
                val action = FirstFrameRecoveryPolicy.action(
                    elapsedMs = elapsed,
                    hasBackup = s.lines.indices.any { it != s.activeLine },
                    onMedia3 = onMedia3,
                    vlcAlreadyTried = vlcFallbackTried,
                )
                // 只在档位变严重时动作（阶梯单调升档）。
                if (!firstFrameLadder.acceptEscalation(generation, action)) continue
                when (action) {
                    FirstFrameRecoveryPolicy.Action.WAIT -> Unit
                    FirstFrameRecoveryPolicy.Action.PREP_BACKUP ->
                        OtvLog.i("点播首帧 ${elapsed}ms 未出 → 预备备用线（不打断）")
                    FirstFrameRecoveryPolicy.Action.ENGINE_FALLBACK -> {
                        // 只剩一条线（或换线更可能无效）且还在 media3：换引擎。
                        vlcFallbackTried = true
                        OtvLog.w("点播首帧 ${elapsed}ms 未出且无备用线 → 切 libVLC 兜底引擎")
                        telemetry.mark(
                            PlaybackTelemetry.Stage.ENGINE_FALLBACK,
                            errorClass = PlaybackTelemetry.ErrorClass.DECODE,
                            detail = "first frame ${elapsed}ms → vlc",
                        )
                        switchEngine("vlc")
                    }
                    FirstFrameRecoveryPolicy.Action.SWITCH -> {
                        val next = pickNextLineForVod()
                        if (next == null) {
                            // 没有可切的线了，但阶梯还没到硬超时：换引擎再试一次
                            // （media3 硬解挂死而线路只有一条是常见组合）。
                            if (onMedia3 && !vlcFallbackTried) {
                                vlcFallbackTried = true
                                OtvLog.w("点播首帧 ${elapsed}ms 未出且无线可切 → 切 libVLC 兜底引擎")
                                telemetry.mark(
                                    PlaybackTelemetry.Stage.ENGINE_FALLBACK,
                                    errorClass = PlaybackTelemetry.ErrorClass.DECODE,
                                    detail = "first frame ${elapsed}ms no line → vlc",
                                )
                                switchEngine("vlc")
                            } else {
                                OtvLog.w("点播首帧 ${elapsed}ms 未出且已无线可切 → 等硬超时收口")
                            }
                        } else {
                            OtvLog.w("点播首帧 ${elapsed}ms 未出 → 切备用线 #$next")
                            telemetry.mark(
                                PlaybackTelemetry.Stage.AUTO_SWITCH,
                                detail = "first frame ${elapsed}ms → line $next",
                            )
                            markAutoSwitch(next)
                            viewModelScope.launch { startSession(next, 0L) }
                        }
                    }
                    FirstFrameRecoveryPolicy.Action.HARD_TIMEOUT -> {
                        // 硬超时不再直接走 onLineFail：那条链第一步是「同线直连重试」，
                        // 而 15s 一个画面都没出恰恰说明同线重试没有意义（地址没取到，
                        // 或者取到了也解不出来帧），只会让用户再白等一轮 15s。
                        // 这里按策略先换线、再换引擎，都用尽才判死。
                        val next = pickNextLineForVod()
                        val action = FirstFrameRecoveryPolicy.hardTimeoutAction(
                            hasBackupLine = next != null,
                            onMedia3 = onMedia3,
                            vlcAlreadyTried = vlcFallbackTried,
                        )
                        when (action) {
                            FirstFrameRecoveryPolicy.HardTimeoutAction.SWITCH_LINE -> {
                                OtvLog.e("点播首帧 ${elapsed}ms 硬超时 → 换备用线 #$next")
                                telemetry.mark(
                                    PlaybackTelemetry.Stage.AUTO_SWITCH,
                                    detail = "first frame hard timeout ${elapsed}ms → line $next",
                                )
                                markAutoSwitch(next!!)
                                viewModelScope.launch { startSession(next, 0L) }
                            }
                            FirstFrameRecoveryPolicy.HardTimeoutAction.ENGINE_FALLBACK -> {
                                vlcFallbackTried = true
                                OtvLog.e("点播首帧 ${elapsed}ms 硬超时且无线可切 → 切 libVLC 兜底引擎")
                                telemetry.mark(
                                    PlaybackTelemetry.Stage.ENGINE_FALLBACK,
                                    errorClass = PlaybackTelemetry.ErrorClass.DECODE,
                                    detail = "first frame hard timeout ${elapsed}ms → vlc",
                                )
                                switchEngine("vlc")
                            }
                            FirstFrameRecoveryPolicy.HardTimeoutAction.GIVE_UP -> {
                                OtvLog.e("点播首帧 ${elapsed}ms 硬超时且已无线可切/换引擎 → 判死")
                                telemetry.mark(
                                    PlaybackTelemetry.Stage.FATAL,
                                    errorClass = PlaybackTelemetry.ErrorClass.DECODE,
                                    detail = "first frame hard timeout ${elapsed}ms exhausted",
                                )
                                onLineFail("画面无输出（首帧超时 ${elapsed}ms）")
                            }
                        }
                        return@launch
                    }
                    FirstFrameRecoveryPolicy.Action.DONE -> return@launch
                }
            }
        }
    }

    /**
     * 首帧阶梯切线时的候选选择：跳过已试过的线，并避开「刚失败过的那条线」。
     *
     * 冷却窗口内的回切会被 [LongPlaybackHealthPolicy.canSwitchNow] 拦下——同一条线
     * 刚失败就立刻切回去，在 20s 内很容易把三条线耗光（比卡住更糟）。
     */
    private fun pickNextLineForVod(): Int? {
        val s = _ui.value
        val cur = s.activeLine
        val now = android.os.SystemClock.elapsedRealtime()
        val candidates = s.lines.indices.filter { it != cur }
        // 先选没试过的，再退而求其次选试过的（但仍要过冷却）。
        return candidates.firstOrNull { it !in triedLines && canAutoSwitchTo(it, now) }
            ?: candidates.firstOrNull { canAutoSwitchTo(it, now) }
    }

    /** 自动切线是否允许（切线冷却未挡住刚失败的那条线）。用户手动切线不受此限。 */
    private fun canAutoSwitchTo(candidateIndex: Int, nowMs: Long): Boolean =
        LongPlaybackHealthPolicy.canSwitchNow(
            nowMs = nowMs,
            lastSwitchAtMs = lastAutoSwitchAt,
            candidateIndex = candidateIndex,
            failedIndex = _ui.value.activeLine,
        )

    /**
     * 记录一次**自动**切线，作为 20–30s 冷却的起点。
     *
     * 冷却只拦「切回刚失败/刚切过的那一条」，切到别的线路永远放行——否则冷却会把
     * 恢复链锁死（只剩刚那条线可用时反而不能自救）。
     */
    private fun markAutoSwitch(targetIndex: Int) {
        if (targetIndex == _ui.value.activeLine) return
        lastAutoSwitchAt = android.os.SystemClock.elapsedRealtime()
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
            var lastBuf = -1L
            var ticks = 0
            // 缓冲/停滞的连续累计时长（毫秒）：交给 LongPlaybackHealthPolicy 判档。
            var bufferingMs = 0L
            // 已在当前缓冲周期里触发过的最高档位，避免同一周期反复重复动作。
            var lastBufferAction = LongPlaybackHealthPolicy.Action.HEALTHY
            while (isActive) {
                delay(1_000)
                val s = _ui.value
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
                } else if (s.playState == EnginePlayState.READY && s.isPlaying) {
                    if (bufferingMs > 0) {
                        telemetry.mark(PlaybackTelemetry.Stage.BUFFER_END, detail = "buf=${s.bufferedAheadMs}ms")
                    }
                    bufferingMs = 0
                    lastBufferAction = LongPlaybackHealthPolicy.Action.HEALTHY
                    ticks = if (s.positionMs == lastPos) ticks + 1 else 0
                    lastPos = s.positionMs
                    if (ticks >= STALL_TICKS_LIMIT) {
                        ticks = 0
                        recoverPlaybackStall("READY 但位置停滞 ${STALL_TICKS_LIMIT}s")
                    }
                } else {
                    ticks = 0
                    lastPos = -1
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
                if (playing && dec != null && lastRendered >= 0) {
                    val advanced = es.positionMs - lastPos
                    if (dec.rendered > lastRendered) frozenTicks = 0
                    else if (advanced >= HEALTH_FREEZE_MIN_ADVANCE_MS) frozenTicks++ else frozenTicks = 0
                } else {
                    frozenTicks = 0
                }
                lastRendered = dec?.rendered ?: -1
                lastPos = es.positionMs
                // 僵尸源修复（见 onSessionStarted）：稳定出画满 60s 才恢复同线重试配额
                if (playing) {
                    healthyTicks++
                    if (healthyTicks >= HEALTH_HEALTHY_TICKS) {
                        healthyTicks = 0
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
                    // 之前这里走的是通用 recoverPlaybackStall，而那条阶梯第 0 步是
                    // RELOAD_CURRENT（原地重试同一条线）——对解码/渲染管线挂死毫无意义，
                    // 只会让用户多等一轮。改为按 LongPlaybackHealthPolicy 的 DECODE 判定降级引擎。
                    val isFrozen = LongPlaybackHealthPolicy.isFrozen(
                        positionMoving = es.positionMs > lastPos,
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

    /** Bounded live/VOD recovery ladder shared with the mobile client. */
    private fun recoverPlaybackStall(reason: String) {
        if (!hasEngine() || _ui.value.fatalMsg != null) return
        val s = _ui.value
        val kind = if (s.isLive) PlaybackKind.LIVE else PlaybackKind.VOD
        val attempt = recoveryAttempt++
        val action = PlaybackRecoveryPolicy.action(kind, attempt)
        val resume = PlaybackRecoveryPolicy.resumePosition(s.positionMs)
        val current = s.activeLine
        // 长时体检：交给恢复阶梯前先留现场（排查方案 §22 字段清单）
        runCatching { com.qiubo.optimaltv.playback.PlaybackHealthLog.stall(healthSnapshot(reason)) }
        OtvLog.w("recovery kind=$kind step=$attempt action=$action reason=$reason pos=${s.positionMs}")
        when (action) {
            RecoveryAction.RELOAD_CURRENT -> {
                engine.seekTo(resume)
                engine.play()
            }
            RecoveryAction.REFRESH_CURRENT -> {
                s.lines.getOrNull(current)?.playRef?.takeIf { it.isNotBlank() }?.let(repo::bustPlayCache)
                viewModelScope.launch { startSession(current, if (s.isLive) 0L else resume) }
            }
            RecoveryAction.SWITCH_SOURCE -> {
                val next = pickNextLineForVod()
                if (next != null) {
                    markAutoSwitch(next)
                    viewModelScope.launch { startSession(next, if (s.isLive) 0L else resume) }
                } else {
                    recreateEngineForRecovery(current, resume)
                }
            }
            RecoveryAction.RECREATE_ENGINE -> recreateEngineForRecovery(current, resume)
            RecoveryAction.RESET_RUNTIME -> {
                com.qiubo.optimaltv.playback.StreamDecryptServer.reset()
                com.qiubo.optimaltv.BackendService.reboot(Graph.appContext, viewModelScope)
                viewModelScope.launch {
                    delay(1_800)
                    startSession(current, 0L)
                }
            }
            RecoveryAction.GIVE_UP -> {
                stopForFatal()
                _ui.value = s.copy(fatalMsg = "播放持续卡顿，已完成自动恢复", controlsVisible = false)
                if (s.isLive) scheduleLiveAutoRetry()
            }
        }
    }

    private fun recreateEngineForRecovery(line: Int, resume: Long) {
        val id = engine.engineId
        runCatching { engine.release() }
        installEngine(EngineRegistry.create(id, Graph.appContext))
        _ui.value = _ui.value.copy(
            engineId = engine.engineId, engineName = engine.displayName, engineEpoch = engineEpochCounter,
        )
        collectEngineState()
        viewModelScope.launch { startSession(line, if (_ui.value.isLive) 0L else resume) }
    }

    /** Manual VOD selection invalidates any older resolver before it can prepare a stale URL. */
    private fun supersedePendingVodSession() {
        sessionGen++
        pendingVodSessionJob?.cancel()
        pendingVodSessionJob = null
        watchdogJob?.cancel()
        stallJob?.cancel()
        healthJob?.cancel()
        firstFrameJob?.cancel()
        vodLadderJob?.cancel()
    }

    /** A VOD source tap also resets the player instance, including same-source retries. */
    private fun recreateEngineForManualSourceChange() {
        if (!hasEngine()) return
        val id = engine.engineId
        val speed = _ui.value.speed
        runCatching { engine.release() }
        installEngine(EngineRegistry.create(id, Graph.appContext))
        runCatching { engine.setSpeed(speed) }
        collectEngineState()
    }

    private suspend fun onSessionStarted(posMs: Long) {
        startedThisSession = true
        recoveryAttempt = 0
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
        // 埋点结算：首帧到达。此处打点后，一行就能读出这次「点播放 → 出画面」各段耗时。
        runCatching {
            val started = _ui.value.lines.getOrNull(_ui.value.activeLine)
            telemetry.mark(
                PlaybackTelemetry.Stage.FIRST_FRAME,
                source = started?.playRef.orEmpty(),
                detail = "line=${_ui.value.activeLine}",
            )
            OtvLog.i(telemetry.summary(started?.playRef.orEmpty()).format())
        }
        // 长时体检基线：起播现场（30s 验收与长跑对比都以这条为参考）
        runCatching { PlaybackHealthLog.start(healthSnapshot("起播")) }
        if (_ui.value.isLive) armIptvAudioCodecCheck()   // 电视#8：无声编解码检查
        // 线路记忆（仅点播；直播始终默认 bb＝原版足球直播2，需求 足球#14）
        if (!_ui.value.isLive) {
            runCatching { settings.rememberLine(vodId, engine.engineId, _ui.value.activeLine) }
            // L0 last_good 记忆：这次成功起播的线路，下次优先直接用它（省掉整轮匹配）。
            // 只存 {mytv_id, episode_index, source_id, content_id}，不存任何地址或凭证。
            runCatching {
                val mytvId = currentItem?.mytvId?.trim()?.toLongOrNull()
                val started = _ui.value.lines.getOrNull(_ui.value.activeLine)
                if (mytvId != null && started != null) {
                    val lineId = repo.lineIdForLastGood(started)
                    repo.rememberLastGoodSource(mytvId, epStart, lineId, db.lastGoodSourceDao())
                }
            }
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

    private suspend fun saveProgress(force: Boolean) {
        val s = _ui.value
        if (s.isLive || isFootballVod) return  // 直播/足球回放不落进度库
        val item = currentItem ?: return
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
        // v1.16：IPTV 频道内备用线路优先（换线路 ≠ 换频道，观众无感）
        if (iptvRotateLine(reason)) return
        if (_ui.value.isLive) {
            // 解码类失败重解析无意义（URL 本身有效，是解码器不行）——直进错误态
            // 等 30s 自动重试。2026-08-30 实测教训：尤文场 bb 流 AVC High 在模拟器
            // 解不动，旧逻辑 3 次同线 fresh 重解析（10s 内密集抓上游）+自动重试连环
            // boot 把 bb 通道解析打成限流不可用，连带其他比赛 bb 回退错源
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
        val next = pickNextLineForVod()
        Log.w(TAG, "line fail(${_ui.value.activeLine}): $reason → ${next ?: "no more lines"}")
        OtvLog.w("line fail(${_ui.value.activeLine}): $reason → ${next?.let { "换线 $it" } ?: "全部失败"}")
        if (next == null) {
            viewModelScope.launch { saveProgress(force = false) }
            stopForFatal()
            _ui.value = _ui.value.copy(fatalMsg = reason, controlsVisible = false)
        } else {
            val pos = _ui.value.positionMs
            markAutoSwitch(next)
            viewModelScope.launch { startSession(next, if (pos > 1_000) pos else 0L) }
        }
    }

    // ---------- 用户操作 ----------

    fun retry() {
        // 需求 足球#2：线路为空（如直播信号未解析出）时 startSession 会数组越界闪退——
        // 此时重试 = 重新走 boot 全链路（重新解析信号源），而不是按线路索引起播。
        // 直播重试同走 fresh（直播稳定性方案修复 2）：服务端 STREAM_CACHE 会把成功结果
        // 缓存 300s，普通重试拿回的还是那个已过期/已受限的 URL → 「信号尚未开播」死循环
        if (_ui.value.lines.isEmpty() || _ui.value.isLive) {
            triedLines.clear()
            liveAutoRetries = 0   // 手动重试重置自动重试配额
            // v1.16 IPTV：手动重试从主线路整队重启（见 iptvRestartFromPrimary）
            if (vodId.startsWith("iptv:") && iptvPrimary != null) {
                iptvRestartFromPrimary()
                return
            }
            _ui.value = _ui.value.copy(fatalMsg = null)
            if (_ui.value.isLive) viewModelScope.launch { boot(fresh = true) }
            else launchSourceBoot(fresh = true)
            return
        }
        val line = _ui.value.activeLine
        val pos = _ui.value.positionMs
        triedLines.clear()
        lastAutoSwitchAt = -1L   // 手动重试 = 显式重置，切线冷却不该拦住
        if (_ui.value.isLive) {
            viewModelScope.launch { startSession(line, if (pos > 1_000) pos else 0L) }
        } else {
            switchLine(line)
        }
    }

    fun switchLine(idx: Int) {
        val state = _ui.value
        if (!ManualLineSelectionPolicy.shouldStart(state.lines.size, state.activeLine, idx, state.isLive)) return
        val pos = state.positionMs
        // 手动切源 = 用户重选起点：清空换线尝试记录——旧记录会把此前起播过的线路
        // 当「已试过」跳过（实测 bb 失败后跳过此前正常播放的 plu，全线耗尽进错误态）
        // 用户一按就停住旧源并进入缓冲态，避免解析新地址期间继续播放旧线路画面。
        if (state.isLive) {
            if (hasEngine()) engine.pause()
        } else {
            supersedePendingVodSession()
            recreateEngineForManualSourceChange()
        }
        _ui.value = _ui.value.copy(
            activeLine = idx,
            fatalMsg = null,
            sourceResolving = !state.isLive,
            playState = EnginePlayState.BUFFERING,
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
        touch()
        viewModelScope.launch {
            startSession(idx, if (pos > 1_000) pos else 0L, forceFresh = !state.isLive)
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

    fun togglePlay() {
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
        _ui.value = _ui.value.copy(panel = p)
        if (p == Panel.MORE && !_ui.value.isLive) refreshEpisodesForPanel()
        touch()
    }

    /** 每次打开更多面板都后台检查新增集数；只增补列表，不重启或移动当前播放。 */
    private fun refreshEpisodesForPanel() {
        if (episodeRefreshJob?.isActive == true) return
        episodeRefreshJob = viewModelScope.launch {
            val current = currentItem ?: return@launch
            val fresh = runCatching {
                repo.resolveDetail(current.id, forceRefresh = true)
            }.getOrNull() ?: return@launch
            val state = _ui.value
            // 线路表只是内存换算（强制刷新已把 detailInfoCache 换成最新一版），不额外打网。
            val refreshedLines = runCatching {
                VisibleVodLinePolicy.apply(repo.resolveLines(fresh, state.epIndex))
            }.getOrDefault(emptyList())
            if (!EpisodeRefreshPolicy.accepts(
                    currentNames = current.episodes.map { it.name },
                    incomingNames = fresh.episodes.map { it.name },
                    currentLines = state.lines.size,
                    incomingLines = refreshedLines.size,
                    keepIndex = state.epIndex,
                )) return@launch
            currentItem = fresh
            _ui.value = state.copy(
                epCount = fresh.episodes.size,
                epNames = fresh.episodes.map { it.name },
                lines = refreshedLines.takeIf { state.activeLine in it.indices } ?: state.lines,
            )
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
        if (ep == cur.epIndex || cur.isLive) return
        supersedePendingVodSession()
        viewModelScope.launch {
            saveProgress(force = true)
            val item = currentItem ?: return@launch
            val newLines = VisibleVodLinePolicy.apply(
                runCatching { repo.resolveLines(item, ep) }.getOrDefault(cur.lines)
            )
            startedThisSession = false
            stallRecoveries = 0
            triedLines.clear()
            // 换集是用户显式动作：上一集的切线冷却不该连累新的一集。
            lastAutoSwitchAt = -1L
            _ui.value = _ui.value.copy(
                epIndex = ep,
                lines = newLines,
                fatalMsg = null,
                playState = EnginePlayState.IDLE,
            )
            val line = cur.activeLine.coerceIn(0, (newLines.size - 1).coerceAtLeast(0))
            startSession(line, 0L)
        }
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
        runCatching { engine.release() }
        renewJob?.cancel()
        liveAutoRetryJob?.cancel()
        super.onCleared()
    }
}

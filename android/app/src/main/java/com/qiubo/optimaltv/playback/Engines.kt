@file:androidx.media3.common.util.UnstableApi

package com.qiubo.optimaltv.playback

import android.content.Context
import android.view.LayoutInflater
import android.view.View
import com.qiubo.optimaltv.R
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.cancel
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.PlaybackParameters
import androidx.media3.common.Player
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.DefaultRenderersFactory
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView

enum class EnginePlayState { IDLE, BUFFERING, READY, ENDED, ERROR }

data class EngineUiState(
    val state: EnginePlayState = EnginePlayState.IDLE,
    val positionMs: Long = 0,
    val durationMs: Long = 0,
    /** 正向缓冲量（黑屏/卡顿诊断与直播续签缓冲保护用） */
    val bufferedAheadMs: Long = 0,
    val isPlaying: Boolean = false,
    val speed: Float = 1f,
    val errorMsg: String? = null,
    /** v1.15 首帧看门狗信号：当前媒体是否已真实渲染出画面。READY 但长时间无首帧 =
     *  解码器挂死不报错（MuMu MediaCodec 已知形态，DECODING_FAILED 回调无从触发）。
     *  每次 prepare/换源重置。 */
    val firstFrameRendered: Boolean = false,
)

class PrepareRequest(val url: String, val startMs: Long)

/**
 * 引擎统一抽象缝（技术方案 §3.2，jellyplay 简化版）：
 * MediaEngine ⇄ IJKPlayer ⇄ libVLC 三引擎可切；当前内置 Media3，IJK/VLC 为预留位。
 */
interface MediaEngine {
    val engineId: String
    val displayName: String
    val renderView: View
    val uiState: StateFlow<EngineUiState>
    fun prepare(request: PrepareRequest)
    /** 直播热切换（不重建播放器/不换渲染面）：换源后直接跳默认位置（直播=边缘）。
     *  autoplay=false：暂停中的静默续签换源（保持暂停，尊重用户操作）。 */
    fun swapSource(url: String, autoplay: Boolean = true)
    /** 跳到默认位置：直播=跳直播边缘（卡顿时弃掉落后位置追最新画面） */
    fun seekToLiveEdge()
    /** 硬解失败兜底：重建播放器改用软解优先的解码器选择（直播 DECODING_FAILED 用） */
    fun setSoftwareDecoderPreferred(on: Boolean)
    /** v1.17 IPTV 大缓冲档（用户需求：美国/英国等远端源卡顿）——加大 min/max 缓冲
     *  平滑抖动；默认关闭（足球直播走本地中继无需）。须在首次 prepare 前设置。 */
    fun setLargeBufferPreferred(on: Boolean) {}
    /**
     * 直播窗口提示（2026-09-16 长跑卡顿根治）：上层在起播前探测媒体清单的可播放窗口
     * 长度（毫秒）后传入，0 = 未知（沿用旧行为）。须在 prepare 前调用。
     *
     * 背景：v1.23 把直播目标延迟统一设成「落后 15s」——对窗口 30s+ 的公网源是稳的，
     * 但足球直播走本地中继时上游清单只有 3×2s＝6s 窗口，15s 目标根本不可达：
     * ExoPlayer 的速度控制会一直按 0.97x 追赶一个够不到的偏移，播着播着就被滑动窗口
     * 甩到最老分片之外（实测 2–5 分钟一次可感知卡顿，最后整段冻住）。
     * 引擎据此选目标偏移与缓冲档：窗口小 → 目标 ≈ 窗口的 60%，把「延迟几秒」变成
     * 真正可用的缓冲垫。
     */
    fun setLiveWindowHintMs(windowMs: Long) {}
    /** 当前音频轨道编解码（如 "mp4a.40.2"/"ac-3"）；未选中/未知返回 null。
     *  电视#8 无声源治理：Media3 软解不含 AC-3/EAC-3 时轨道静默失败不报错，
     *  由上层轮询本接口发现后切 libVLC（自带 ffmpeg 软解）。 */
    fun audioCodecs(): String? = null
    /**
     * 解码/渲染计数快照（2026-09-16 长时体检）：`rendered` = 真正送画面的帧数，
     * 是「位置照常前进但画面冻住」（既有看门狗抓不到的形态）的唯一硬信号。
     * 必须【在应用线程（主线程）调用】——ExoPlayer 的取数接口会校验应用线程。
     * 引擎不支持时返回 null（libVLC），调用方只记日志、不做冻结判定。
     */
    fun decoderStats(): DecoderStats? = null
    fun play()
    fun pause()
    fun seekTo(ms: Long)
    fun setSpeed(v: Float)
    fun release()
}

/** 引擎能力位矩阵（设置页展示用） */
data class EngineDescriptor(
    val id: String,
    val displayName: String,
    val available: Boolean,
    val note: String,
)

object EngineRegistry {
    val descriptors = listOf(
        EngineDescriptor("media3", "Media3 / ExoPlayer", true, "默认内核：HLS/DASH 标准流、硬解稳定"),
        EngineDescriptor("vlc", "libVLC 兜底", true, "自带 ffmpeg 软解：MediaCodec 解不动的流（AVC High 8x8）自动兜底"),
        EngineDescriptor("ijk", "IJKPlayer", false, "预留：点播杂源兜底，设备阶段接入"),
    )

    fun descriptorOf(id: String): EngineDescriptor =
        descriptors.firstOrNull { it.id == id } ?: descriptors[0]

    /** 未内置引擎一律回落 Media3（AV1 无硬解时的软解兜底同理） */
    fun create(engineId: String, context: Context): MediaEngine =
        if (engineId == "vlc") VlcEngine(context) else Media3Engine(context)
}

/**
 * Media3(ExoPlayer) 实现：
 * - setEnableDecoderFallback(true)：解码失败自动转软解（AV1 无硬解时兜底，方案 §4.6-⑤）
 * - 音频焦点交给 ExoPlayer 托管（handleAudioFocus=true，方案 §3.4）
 * - 容器解析容错由 DefaultMediaSourceFactory 内建
 */
class Media3Engine(context: Context) : MediaEngine {

    override val engineId = "media3"
    override val displayName = "Media3 / ExoPlayer"

    private val appCtx: Context = context.applicationContext
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val _ui = MutableStateFlow(EngineUiState())
    override val uiState: StateFlow<EngineUiState> = _ui.asStateFlow()

    private var player: ExoPlayer? = null
    private var ticker: Job? = null
    /** 累计丢帧数（黑屏诊断：解码/供给不足的量化证据） */
    @Volatile private var droppedFramesTotal = 0
    /** 播放中 rebuffer 次数（v1.14 卡顿量化：READY→BUFFERING 且正在播放） */
    @Volatile private var rebufferCount = 0
    /** 软解优先（直播硬解失败兜底）：true 时 MediaCodecSelector 只选软解（google/c2.android），
     *  无软解才回落硬解列表。切换时重建播放器（渲染面复用不闪黑）。 */
    @Volatile private var softwarePreferred = false
    /** v1.17 IPTV 大缓冲档：ensurePlayer 按 this 标记选 LoadControl 参数 */
    @Volatile private var largeBuffer = false
    /** 直播窗口长度提示（0 = 未知）：见 MediaEngine.setLiveWindowHintMs */
    @Volatile private var liveWindowHintMs = 0L

    override fun setLargeBufferPreferred(on: Boolean) {
        largeBuffer = on
    }

    /** 小窗口直播（足球直播中继：3×2s＝6s）：必须换掉固定的 15s 目标延迟与 6s 恢复水位 */
    private fun smallWindowLive(): Boolean = LiveWindowPolicy.isSmall(liveWindowHintMs)

    override fun setLiveWindowHintMs(windowMs: Long) {
        val v = if (windowMs > 0) windowMs else 0L
        if (v == liveWindowHintMs) return
        val profileChanged = smallWindowLive() != LiveWindowPolicy.isSmall(v)
        com.qiubo.optimaltv.OtvLog.i(
            "engine 直播窗口提示 ${liveWindowHintMs}ms → ${v}ms" +
                if (profileChanged) "（重建播放器换缓冲档）" else "",
        )
        liveWindowHintMs = v
        // 缓冲档（LoadControl）只能在建播放器时给——档位变化时释放，下次 prepare 以新档重建
        if (profileChanged) releasePlayerForRebuild()
    }

    /**
     * Detach PlayerView and listeners before releasing ExoPlayer. PlayerView may still be in the
     * Compose tree during a same-frame source/engine rebuild; releasing first leaves the attached
     * view observing a dead player and can race its next measure/draw callbacks.
     */
    private fun releasePlayerInstance() {
        val oldPlayer = player ?: return
        player = null
        runCatching { oldPlayer.removeListener(listener) }
        runCatching { oldPlayer.removeAnalyticsListener(analyticsListener) }
        runCatching { renderViewAsPlayer().player = null }
            .onFailure { com.qiubo.optimaltv.OtvLog.w("engine detach PlayerView failed: ${it.message}") }
        runCatching { oldPlayer.release() }
            .onFailure { com.qiubo.optimaltv.OtvLog.w("engine release failed: ${it.message}") }
    }

    /** 释放当前播放器实例（渲染面/监听器复用），下次 ensurePlayer 以新参数重建 */
    private fun releasePlayerForRebuild() = releasePlayerInstance()

    override fun audioCodecs(): String? = runCatching { player?.audioFormat?.codecs }.getOrNull()

    /**
     * 长时体检（2026-09-16）：ExoPlayer 视频渲染器计数。
     *
     * `renderedOutputBufferCount` 是真正送进渲染面的帧数——位置前进而它不增即解码/渲染
     * 管线停摆（线上「20 分钟后画面冻住、退出重进才好」的候选形态）。
     * getVideoDecoderCounters() 内部 verifyApplicationThread()，本方法只能在主线程调用
     * （体检任务跑在 viewModelScope 默认的 Main 调度器上）。
     */
    override fun decoderStats(): DecoderStats? = runCatching {
        player?.videoDecoderCounters?.let { c ->
            DecoderStats(
                rendered = c.renderedOutputBufferCount,
                skipped = c.skippedOutputBufferCount,
                dropped = c.droppedBufferCount,
                droppedInput = c.droppedInputBufferCount,
                decoderInits = c.decoderInitCount,
                consecutiveDropped = c.maxConsecutiveDroppedBufferCount,
            )
        }
    }.getOrNull()

    override val renderView: View by lazy {
        val inflationParent = android.widget.FrameLayout(appCtx)
        (LayoutInflater.from(appCtx).inflate(R.layout.view_player_texture, inflationParent, false) as PlayerView).apply {
            // 渲染层只负责显示画面，不参与 TV 焦点搜索；遥控器焦点必须留在 Compose 控制层
            isFocusable = false
            isFocusableInTouchMode = false
            descendantFocusability = android.view.ViewGroup.FOCUS_BLOCK_DESCENDANTS
            importantForAccessibility = android.view.View.IMPORTANT_FOR_ACCESSIBILITY_NO
            layoutParams = android.view.ViewGroup.LayoutParams(
                android.view.ViewGroup.LayoutParams.MATCH_PARENT,
                android.view.ViewGroup.LayoutParams.MATCH_PARENT,
            )
        }
    }

    private val listener = object : Player.Listener {
        override fun onPlaybackStateChanged(playbackState: Int) {
            val mapped = when (playbackState) {
                Player.STATE_IDLE -> EnginePlayState.IDLE
                Player.STATE_BUFFERING -> EnginePlayState.BUFFERING
                Player.STATE_READY -> EnginePlayState.READY
                Player.STATE_ENDED -> EnginePlayState.ENDED
                else -> EnginePlayState.IDLE
            }
            // 播放中 READY→BUFFERING = 一次可感知 rebuffer（v1.14 卡顿量化指标）
            if (mapped == EnginePlayState.BUFFERING && _ui.value.state == EnginePlayState.READY &&
                _ui.value.isPlaying
            ) {
                rebufferCount++
                com.qiubo.optimaltv.OtvLog.w("engine rebuffer #$rebufferCount（播放中转缓冲，buf=${_ui.value.bufferedAheadMs}ms）")
            }
            com.qiubo.optimaltv.OtvLog.i("engine state → $mapped")
            _ui.value = _ui.value.copy(state = mapped)
        }

        override fun onIsPlayingChanged(isPlaying: Boolean) {
            _ui.value = _ui.value.copy(isPlaying = isPlaying)
        }

        override fun onPlaybackParametersChanged(params: PlaybackParameters) {
            _ui.value = _ui.value.copy(speed = params.speed)
        }

        override fun onPlayerError(error: PlaybackException) {
            // v1.23（2026-09-06 直播卡顿根治）：BEHIND_LIVE_WINDOW 用官方推荐动作
            // （seekToDefaultPosition + re-prepare）就地自愈——~0.5s 跳边缘续播。
            // 旧版上抛走 VM 的 fresh 重解析阶梯（1-2s 频道页+播放器页+解密），
            // 且根因（起播 seekTo(0) 落窗口头）不除时会每 15-80s 循环重演
            //（实测 5 分钟 6 次，观感=周期性大卡顿）。
            if (error.errorCode == PlaybackException.ERROR_CODE_BEHIND_LIVE_WINDOW) {
                com.qiubo.optimaltv.OtvLog.w("engine 落后直播窗口 → 跳直播边缘就地自愈")
                _ui.value = _ui.value.copy(state = EnginePlayState.BUFFERING, errorMsg = null)
                player?.let { p ->
                    runCatching {
                        p.seekToDefaultPosition()
                        p.prepare()
                        p.playWhenReady = true
                    }
                }
                return
            }
            com.qiubo.optimaltv.OtvLog.e("engine playerError: ${error.errorCodeName} ${error.message}")
            _ui.value = _ui.value.copy(
                state = EnginePlayState.ERROR,
                errorMsg = error.errorCodeName + ": " + (error.message ?: "未知错误"),
            )
        }

        // ---- 黑屏/卡顿诊断日志（需求 足球#3：排查「莫名其妙的黑屏」）----
        override fun onRenderedFirstFrame() {
            // 关键信号：解码器吐出第一帧 = 画面真出来了；READY 但首帧迟迟不到 = 黑屏
            com.qiubo.optimaltv.OtvLog.i("engine 首帧已渲染（画面输出）")
            _ui.value = _ui.value.copy(firstFrameRendered = true)
        }

        override fun onVideoSizeChanged(videoSize: androidx.media3.common.VideoSize) {
            com.qiubo.optimaltv.OtvLog.i(
                "engine 视频轨道 ${videoSize.width}x${videoSize.height} fps=${player?.videoFormat?.frameRate}",
            )
        }
    }

    /** 丢帧统计（AnalyticsListener 才有该回调）：解码/供给不足的量化证据 */
    private val analyticsListener = object : androidx.media3.exoplayer.analytics.AnalyticsListener {
        override fun onVideoDecoderInitialized(
            eventTime: androidx.media3.exoplayer.analytics.AnalyticsListener.EventTime,
            decoderName: String,
            initializedTimestampMs: Long,
            initializationDurationMs: Long,
        ) {
            com.qiubo.optimaltv.OtvLog.i("engine 视频解码器 $decoderName init=${initializationDurationMs}ms")
        }

        override fun onDroppedVideoFrames(
            eventTime: androidx.media3.exoplayer.analytics.AnalyticsListener.EventTime,
            droppedFrames: Int,
            elapsedMs: Long,
        ) {
            droppedFramesTotal += droppedFrames
            com.qiubo.optimaltv.OtvLog.w("engine 丢帧 +$droppedFrames（${elapsedMs}ms，累计 $droppedFramesTotal）")
        }
    }

    private fun ensurePlayer(): ExoPlayer {
        player?.let { return it }
        // 软解兜底在渲染层开启（AV1 无硬解时自动降级，方案 §4.6-⑤）
        val renderersFactory = DefaultRenderersFactory(appCtx)
            .setExtensionRendererMode(DefaultRenderersFactory.EXTENSION_RENDERER_MODE_ON)
            .setEnableDecoderFallback(true)
            .let { f ->
                if (softwarePreferred) {
                    f.setMediaCodecSelector { mime, secure, tunneling ->
                        val all = androidx.media3.exoplayer.mediacodec.MediaCodecSelector.DEFAULT
                            .getDecoderInfos(mime, secure, tunneling)
                        val sw = all.filter { info ->
                            info.name.contains("google", true) || info.name.contains("c2.android", true)
                        }
                        com.qiubo.optimaltv.OtvLog.i("软解优先：${sw.size}/${all.size} 个候选（${sw.joinToString(",") { it.name }}）")
                        if (sw.isNotEmpty()) sw else all
                    }
                } else f
            }
        val httpFactory = DefaultHttpDataSource.Factory()
            .setAllowCrossProtocolRedirects(true)
            .setConnectTimeoutMs(10_000)
            // 需求 影视#4：源站/中继链路慢，读超时太短会中途断流重试（观感=经常卡顿）
            .setReadTimeoutMs(30_000)
            .setUserAgent("Mozilla/5.0 (Linux; Android 11) OptimalTV/1.0")
        val dataSourceFactory = DefaultDataSource.Factory(appCtx, httpFactory)
        // 需求 影视#4：加大缓冲池——低配盒子/慢源下 15s 缓冲一低于水位就 rebuffer；
        // v1.10（2026-08-31 低端机起播提速）：起播水位 2.5s→1s、二次缓冲 5s→2s——
        // 缓冲水位只决定「多少缓冲开始出画」，min/max 不变，播放中稳定性不受影响。
        // v1.14（2026-09-01 卡顿根治）：max 90s→120s——直播续签改「旧地址死透才切」后，
        // 检测间隔 90s 必须被缓冲余量完整覆盖（120s > 90s 检测 + 30s 切换衔接安全垫）。
        // v1.17：IPTV（远端公网源，美英线抖动大）用大缓冲档 45s/150s；
        // v1.19（电视#3 换台提速）：大缓冲档起播水位 3s/6s→1s/2s——水位只影响「出画
        // 门槛」，min/max 不变；实测 IPTV 换台转圈时间主要耗在 3s 起播水位上。
        // v1.22（2026-09-05 需求⑨ IPTV 卡顿）：rebuffer 水位 2s→6s——公网源瞬时抖动
        // （1-3s 断供）在 2s 水位下每次都演成可感知暂停，6s 让 ExoPlayer 在缓冲余量内
        // 自愈（观感=不再频繁转圈），恢复代价仅多等几秒缓冲。
        // v1.23（2026-09-06 足球半小时零卡顿）：默认档 rebuffer 4s→6s 与 IPTV 档对齐
        // ——直播 CDN 3-5s 级供给抖动在 4s 水位下仍会露头成可感知转圈，6s 水位下
        // 播放器在缓冲余量内自愈；点播同享（恢复多等 2s，换感知不到的抖动）。
        // 点播与足球直播（本地中继）维持 30s/120s。
        val (minBuf, maxBuf, startBuf, reBuf) =
            when {
                // 小窗口直播（中继足球源 6s）：恢复水位必须小于窗口，否则一次抖动就要
                // 等满整个窗口才出画（实测「转圈 8s 后触发整会话重解析」的大卡顿）
                smallWindowLive() -> intArrayOf(8_000, 16_000, 600, 1_500).toList()
                largeBuffer -> intArrayOf(45_000, 150_000, 1_000, 6_000).toList()
                else -> intArrayOf(30_000, 120_000, 1_000, 6_000).toList()
            }
        val loadControl = DefaultLoadControl.Builder()
            .setBufferDurationsMs(minBuf, maxBuf, startBuf, reBuf)
            .setPrioritizeTimeOverSizeThresholds(true)
            .build()
        val p = ExoPlayer.Builder(appCtx, renderersFactory)
            // v1.14 卡顿根治：签名过期/防盗链类 4xx 重试必同果——默认策略的指数退避
            // 只会把「该换地址」拖成几十秒的反复 rebuffer（观感=频繁卡顿）。
            // 快速上抛给 PlayerViewModel 阶梯（fresh 重解析/换线/中继↔直连切换）。
            .setMediaSourceFactory(
                DefaultMediaSourceFactory(dataSourceFactory)
                    .setLoadErrorHandlingPolicy(FastFailLoadErrorPolicy()),
            )
            .setLoadControl(loadControl)
            .setSeekBackIncrementMs(30_000)
            .setSeekForwardIncrementMs(30_000)
            .build()
        p.setAudioAttributes(
            AudioAttributes.Builder()
                .setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_MOVIE)
                .build(),
            /* handleAudioFocus = */ true,
        )
        p.addListener(listener)
        p.addAnalyticsListener(analyticsListener)
        renderViewAsPlayer().player = p
        player = p
        startTicker()
        return p
    }

    private fun renderViewAsPlayer(): PlayerView = renderView as PlayerView

    private fun startTicker() {
        ticker?.cancel()
        ticker = scope.launch {
            var beat = 0
            while (isActive) {
                player?.let { p ->
                    _ui.value = _ui.value.copy(
                        // v1.23（2026-09-06 直播卡顿根治）：position 不再 coerceAtLeast(0)——
                        // 带 PROGRAM-DATE-TIME 的直播清单（VCLOUD 系实测）currentPosition 为
                        // 负值（落后直播边缘 N 秒），旧版把负值恒压成 0 → VM stall 监测
                        // 「位置 8s 不动」每轮误判 → seekToLiveEdge+rebuffer 每 8s 循环
                        //（画面正常却在自伤式重置，用户观感=持续小卡顿）。
                        // 直播页不渲染进度条、直播不落进度库，负值透传无副作用；
                        // 点播 position 本就非负，行为不变。
                        positionMs = p.currentPosition,
                        durationMs = if (p.duration > 0) p.duration else 0,
                        bufferedAheadMs = p.totalBufferedDuration.coerceAtLeast(0),
                    )
                    // 播放心跳（每 10s 一条）：黑屏事后排查的核心证据链——
                    // state=READY 但持续 isPlaying=true 且首帧日志缺失 = 渲染层黑屏；
                    // 结合缓冲、实际输出与丢帧计数区分供给不足和解码/渲染压力。
                    if (++beat % 20 == 0) {
                        val st = _ui.value
                        com.qiubo.optimaltv.OtvLog.i(
                            "hb ${st.state} playing=${st.isPlaying} pos=${st.positionMs / 1000}s " +
                                "buf=${st.bufferedAheadMs / 1000}s " +
                                "dropped=${p.videoDecoderCounters?.droppedBufferCount ?: droppedFramesTotal} rbuf=$rebufferCount " +
                                "rendered=${p.videoDecoderCounters?.renderedOutputBufferCount ?: -1} " +
                                "skipped=${p.videoDecoderCounters?.skippedOutputBufferCount ?: -1} " +
                                "speed=${p.playbackParameters.speed} liveOffsetMs=${p.currentLiveOffset}",
                        )
                    }
                }
                delay(500)
            }
        }
    }

    /** MediaItem 构建（直播稳定性修复 2026-08-30）：中继 URL 路径是 /api/relay?u=…，
     *  扩展名藏在编码后的查询参数里——ExoPlayer 按路径推断内容类型会判成普通媒体，
     *  解析 HLS 文本失败报 PARSING_CONTAINER_UNSUPPORTED（「直播看不了」根因：
     *  VOD 直链带 .m3u8 路径所以一直正常，直播中继全军覆没）。
     *  URL 任意位置含 m3u8 即显式声明 HLS MIME。
     *  v1.23（2026-09-06 半小时零卡顿）：直播显式 LiveConfiguration——默认目标偏移仅
     *  约 3×分片时长（实测 4-8s），CDN 瞬时断供几秒就击穿成可感知 rebuffer；
     *  目标 15s（播放器在 [8s,30s] 区间自适配：抖动时自动退离边缘吸收，稳定时追近）
     *  观感=固定落后十来秒的稳定直播。点播静态清单（带 ENDLIST）不受影响。 */
    private fun mediaItemFor(url: String): MediaItem {
        val b = MediaItem.Builder().setUri(url)
        if (url.contains("m3u8", ignoreCase = true)) {
            b.setMimeType(androidx.media3.common.MimeTypes.APPLICATION_M3U8)
            b.setLiveConfiguration(liveConfigurationFor())
        }
        return b.build()
    }

    /**
     * 直播目标偏移（2026-09-16 长跑卡顿根治）：
     * - 窗口未知 / 大窗口（公网 IPTV 等，窗口通常 30s+）：沿用 v1.23 的「落后 15s」，
     *   抖动时自动退离边缘吸收，稳定时追近；
     * - 小窗口（中继足球源实测 6s）：目标偏移取窗口的 60%（6s → 3.6s），多留一段抗抖缓冲。
     *   **可达**——不可达时 ExoPlayer 的速度控制会长期按 0.97x 追赶，最后被滑动窗口
     *   甩出最老分片，表现为「看十几分钟后卡死，退出重进才好」。
     */
    private fun liveConfigurationFor(): MediaItem.LiveConfiguration {
        val w = liveWindowHintMs
        val cfg = MediaItem.LiveConfiguration.Builder()
        return if (smallWindowLive()) {
            cfg.setTargetOffsetMs(LiveWindowPolicy.targetOffsetMs(w))
                .setMinOffsetMs(LiveWindowPolicy.minOffsetMs(w))
                .setMaxOffsetMs(LiveWindowPolicy.maxOffsetMs(w))
                .build()
        } else {
            cfg.setTargetOffsetMs(15_000).setMinOffsetMs(8_000).setMaxOffsetMs(30_000).build()
        }
    }

    override fun prepare(request: PrepareRequest) {
        val p = ensurePlayer()
        _ui.value = _ui.value.copy(
            state = EnginePlayState.BUFFERING,
            positionMs = request.startMs,
            durationMs = 0,
            errorMsg = null,
            firstFrameRendered = false,   // 新媒体：首帧信号复位（首帧看门狗判定基准）
        )
        p.setMediaItem(mediaItemFor(request.url))
        // v1.23（2026-09-06 直播起播落窗修复）：startMs<=0 一律 seekToDefaultPosition——
        // 直播 seekTo(0) = 跳到滑动窗口头（最老分片），供给稍慢就被窗口滚出 →
        // ERROR_CODE_BEHIND_LIVE_WINDOW 循环（实测每 15-80s 一次重解析级大卡顿）；
        // default position = 直播边缘附近（LiveConfiguration targetOffset 内）。
        // 点播 default = 片头，与 seekTo(0) 等效；startMs>0（点播续播）行为不变。
        if (request.startMs > 0) p.seekTo(request.startMs) else p.seekToDefaultPosition()
        p.prepare()
        p.playWhenReady = true
    }

    override fun swapSource(url: String, autoplay: Boolean) {
        val p = ensurePlayer()
        com.qiubo.optimaltv.OtvLog.i("engine 热切换源（不重建播放器, autoplay=$autoplay）: ${url.take(110)}")
        _ui.value = _ui.value.copy(state = EnginePlayState.BUFFERING, errorMsg = null, firstFrameRendered = false)
        p.setMediaItem(mediaItemFor(url))
        p.prepare()
        p.playWhenReady = autoplay
        p.seekToDefaultPosition()   // 直播：直接跳默认位置（≈直播边缘），不从窗口头播
    }

    override fun seekToLiveEdge() {
        player?.seekToDefaultPosition()
    }

    override fun setSoftwareDecoderPreferred(on: Boolean) {
        if (softwarePreferred == on) return
        softwarePreferred = on
        com.qiubo.optimaltv.OtvLog.w("engine 解码器策略切换：软解优先=$on（重建播放器）")
        // 立即重建：释放当前实例，下次 prepare 以新选择器构建（渲染面/监听器复用）
        releasePlayerForRebuild()
    }

    override fun play() {
        val p = player ?: return
        p.play()
        // 乐观镜像：play()/pause() 同步改 playWhenReady，但 onIsPlayingChanged 事件要
        // 下一拍才到——快速连按时 UI 读镜像拿到旧值会连按同向（播放/暂停按钮与实际
        // 播放状态不一致的根因）。先按意图写镜像，引擎事件随后校正。
        _ui.value = _ui.value.copy(isPlaying = true)
    }

    override fun pause() {
        val p = player ?: return
        p.pause()
        _ui.value = _ui.value.copy(isPlaying = false)
    }

    override fun seekTo(ms: Long) { player?.seekTo(ms.coerceAtLeast(0)) }

    override fun setSpeed(v: Float) {
        player?.playbackParameters = PlaybackParameters(v)
    }

    override fun release() {
        scope.cancel()
        releasePlayerInstance()
    }
}

/**
 * 出站加载错误策略（v1.14 卡顿根治配套）：
 * - 403/404/410（签名过期/防盗链/资源不归）：重试同一 URL 必然同果——直接放弃重试，
 *   快速上抛为播放错误，交给 PlayerViewModel 的阶梯（fresh 重解析 / 中继↔直连切换 /
 *   换线）。默认策略的指数退避（5s/10s/…）会把「该换地址」拖成长时间反复 rebuffer。
 * - 其余（5xx/网络抖动）：维持默认退避（瞬时故障 ExoPlayer 自愈最合适）。
 */
private class FastFailLoadErrorPolicy : androidx.media3.exoplayer.upstream.LoadErrorHandlingPolicy {

    private val fallback = androidx.media3.exoplayer.upstream.DefaultLoadErrorHandlingPolicy()

    override fun getRetryDelayMsFor(
        loadErrorInfo: androidx.media3.exoplayer.upstream.LoadErrorHandlingPolicy.LoadErrorInfo,
    ): Long {
        val e = loadErrorInfo.exception
        if (e is androidx.media3.datasource.HttpDataSource.InvalidResponseCodeException) {
            when (e.responseCode) {
                403, 404, 410 -> return C.TIME_UNSET   // 不重试：快速上抛
            }
        }
        return fallback.getRetryDelayMsFor(loadErrorInfo)
    }

    override fun getMinimumLoadableRetryCount(dataType: Int): Int =
        fallback.getMinimumLoadableRetryCount(dataType)

    /** 解码器/轨道降级选择：不干预（null=沿用默认，由 RenderersFactory 的 fallback 兜底） */
    override fun getFallbackSelectionFor(
        fallbackOptions: androidx.media3.exoplayer.upstream.LoadErrorHandlingPolicy.FallbackOptions,
        loadErrorInfo: androidx.media3.exoplayer.upstream.LoadErrorHandlingPolicy.LoadErrorInfo,
    ): androidx.media3.exoplayer.upstream.LoadErrorHandlingPolicy.FallbackSelection? = null
}

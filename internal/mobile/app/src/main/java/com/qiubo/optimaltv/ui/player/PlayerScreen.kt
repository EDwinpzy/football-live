package com.qiubo.optimaltv.ui.player

import android.app.Activity
import android.view.WindowManager
import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.Shadow
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.model.formatTime
import com.qiubo.optimaltv.playback.EnginePlayState
import com.qiubo.optimaltv.ui.components.AppleLoading
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.PortraitPlayerControls
import com.qiubo.optimaltv.ui.components.tapCard
import com.qiubo.optimaltv.ui.detail.simpleFactory
import com.qiubo.optimaltv.ui.theme.AccentBlue
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import com.qiubo.optimaltv.ui.theme.TextPrimary
import java.net.URLDecoder
import kotlin.math.roundToInt

/**
 * 播放页（v1.18 浮层视觉与 TV 版 1:1 同款：tvOS 播放器，需求 影视#3 / 足球#13/14）：
 * - 影视：单击画面呼出/隐藏菜单（标题+meta / 更多钮 / 进度条带拖动 seek）；BACK 隐藏菜单，
 *   菜单隐藏时两次返回退出（首次提示）；双击左右 1/3 = ±10s（TV 版为左右键 ±30s 的触控映射）；
 *   更多钮 → 右侧悬浮栏（影片信息+选集+换源+倍速+画幅）
 * - 足球直播（需求 足球#14）：无任何菜单、无比分板；唯一控件=信号源选择条
 *   （单击画面呼出/收起，点 chip 即时换线，BACK/5s 自动隐藏）；两次返回退出
 * - 播放会话与 TV 版共用同一 PlayerViewModel（断点续播/看门狗换线/90s 续签/三段回退全保留）
 */
@Composable
fun PlayerScreen(nav: NavController, vodIdArg: String, epIndex: Int, resumeMs: Long = 0L) {
    val vodId = URLDecoder.decode(vodIdArg, "UTF-8")
    // v1.17 二重门控（公测版，L1 验签分散）：即使路由层门控被绕过，播放页自身也校验——
    // 授权中途失效（如联网复验命中吊销降级）立即退出播放；内测版编译期剔除
    if (com.qiubo.optimaltv.BuildConfig.LICENSE_ENABLED) {
        androidx.compose.runtime.LaunchedEffect(Unit) {
            com.qiubo.optimaltv.license.LicenseManager.state.collect {
                if (it != null && !com.qiubo.optimaltv.license.LicenseManager.isPremium()) {
                    runCatching { nav.popBackStack() }
                }
            }
        }
    }
    val vm: PlayerViewModel = viewModel(
        key = "player:$vodId:$epIndex:$resumeMs",
        factory = simpleFactory {
            PlayerViewModel(vodId, epIndex, Graph.repo, Graph.db, Graph.settings, resumeStartMs = resumeMs)
        },
    )
    val ui by vm.ui.collectAsStateWithLifecycle()

    // KEEP_SCREEN_ON：移动端播放防息屏
    val activity = LocalContext.current as Activity
    DisposableEffect(Unit) {
        activity.window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        onDispose { activity.window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON) }
    }

    // 后台暂停/回来自动恢复
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val obs = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_PAUSE -> vm.onHostPause()
                Lifecycle.Event.ON_RESUME -> vm.onHostResume()
                else -> Unit
            }
        }
        lifecycleOwner.lifecycle.addObserver(obs)
        onDispose { lifecycleOwner.lifecycle.removeObserver(obs) }
    }

    // 控制条/信号源条 5s 无操作自动隐藏
    var tick by remember { mutableIntStateOf(0) }   // 控制条/信号源条自动隐藏计时基准
    LaunchedEffect(ui.controlsVisible, ui.panel, ui.srcPickerVisible, tick) {
        if (ui.panel == Panel.NONE && ui.fatalMsg == null) {
            kotlinx.coroutines.delay(5_000)
            if (ui.srcPickerVisible) vm.hideSrcPicker()
            else if (ui.controlsVisible) vm.hideControls()
        }
    }

    // 返回语义（与 TV 版同款）：面板开 → 关面板；信号源条开（直播）→ 收起；
    // 菜单开 → 隐藏菜单；菜单隐藏 → 2.5s 内两次返回退出（首次提示）
    val backHint = remember { mutableStateOf<String?>(null) }
    LaunchedEffect(backHint.value) {
        if (backHint.value != null) { kotlinx.coroutines.delay(2500); backHint.value = null }
    }
    fun onBackPressed() {
        when {
            ui.panel != Panel.NONE -> vm.setPanel(Panel.NONE)
            ui.srcPickerVisible -> vm.hideSrcPicker()    // 直播：先收信号源条
            ui.fatalMsg != null -> nav.popBackStack()    // 错误兜底态：BACK 直接返回上一页
            ui.controlsVisible -> vm.hideControls()
            else -> nav.popBackStack()   // 总体#5：一次返回直接退出播放
        }
    }

    // 需求12⑥⑦（2026-09-07 竖屏播放器重构）：竖屏=顶部 16:9 视频区（原有全部
    // 浮层/手势/面板限制在视频区内）+ 下方常驻操作区（标题/进度/播放/全屏）；
    // 全屏按钮=请求横屏（App 本身沉浸全屏 = 系统级全屏播放），退出本页恢复方向跟随
    val portrait = androidx.compose.ui.platform.LocalConfiguration.current.orientation ==
        android.content.res.Configuration.ORIENTATION_PORTRAIT
    DisposableEffect(Unit) {
        onDispose { activity.requestedOrientation = android.content.pm.ActivityInfo.SCREEN_ORIENTATION_FULL_USER }
    }

    androidx.compose.foundation.layout.Column(
        Modifier
            .fillMaxSize()
            .background(Color.Black),
    ) {
    Box(
        Modifier
            .fillMaxWidth()
            .then(if (portrait) Modifier.aspectRatio(16f / 9f) else Modifier.weight(1f)),
    ) {
        // 视频渲染层：按画幅模式约束容器比例（key=引擎实例代数；三段回退切引擎重建视图）
        Box(
            Modifier
                .fillMaxSize()
                .then(aspectConstraint(ui.aspectOrdinal)),
            contentAlignment = Alignment.Center,
        ) {
            if (vm.hasEngine()) {
                key(ui.engineEpoch) {
                    AndroidView(
                        factory = {
                            val v = vm.engine.renderView
                            (v.parent as? android.view.ViewGroup)?.removeView(v)
                            // Media3 PlayerView 自带 click 监听会拦截触摸
                            (v as? androidx.media3.ui.PlayerView)?.apply {
                                useController = false
                                isClickable = false
                                isLongClickable = false
                            }
                            v
                        },
                        modifier = Modifier.fillMaxSize(),
                    )
                }
            }
        }

        // 手势层（影视#1 v1.19 常规移动端手势）：单击=浮层开关；双击左右 1/3 = ±15s、
        // 双击中央=播放/暂停；水平拖动=滑动快进（带预览浮层）；竖直拖动=左半亮度/右半音量。
        // 必须是视频层的【兄弟节点且靠后组合】——Compose 命中测试取最前的手势分支，
        // AndroidView（Media3 PlayerView）会拦截祖先链路上的事件，兄弟置顶才收得到
        if (ui.fatalMsg == null) {
            // 滑动快进/亮度音量 手势反馈浮层数据
            var seekPreviewMs by remember { mutableStateOf<Long?>(null) }
            var seekDir by remember { mutableStateOf(1) }                // +1 快进 / -1 快退（决定箭头朝向）
            var vbKind by remember { mutableStateOf<String?>(null) }     // "brightness"/"volume"
            var vbValue by remember { mutableStateOf(0f) }
            val dragCtx = LocalContext.current

            Box(
                Modifier
                    .fillMaxSize()
                    .pointerInput(ui.isLive) {
                        detectTapGestures(
                            onTap = {
                                if (ui.isLive) vm.toggleSrcPicker() else vm.toggleControls()
                                tick++
                            },
                            onDoubleTap = { off ->
                                if (!ui.isLive) {
                                    val third = size.width / 3f
                                    when {
                                        off.x < third -> vm.seekBy(-PlaybackPolicy.SEEK_STEP_MS)
                                        off.x > third * 2f -> vm.seekBy(PlaybackPolicy.SEEK_STEP_MS)
                                        else -> vm.togglePlay()                 // 双击中央：播放/暂停
                                    }
                                    tick++
                                }
                            },
                        )
                    }
                    // 滑动快进（仅点播）：全屏宽 ≈ 120s，拖动中实时预览、松手落定
                    .pointerInput(ui.isLive, ui.durationMs) {
                        if (ui.isLive || ui.durationMs <= 0) return@pointerInput
                        detectHorizontalDragGestures(
                            onDragStart = { seekPreviewMs = ui.positionMs },
                            onHorizontalDrag = { change, dragAmount ->
                                change.consume()
                                if (dragAmount != 0f) seekDir = if (dragAmount > 0f) 1 else -1
                                val cur = seekPreviewMs ?: ui.positionMs
                                val deltaMs = (dragAmount / size.width.toFloat()) * 120_000f
                                seekPreviewMs = (cur + deltaMs.toLong())
                                    .coerceIn(0L, (ui.durationMs - 1_000).coerceAtLeast(0))
                            },
                            onDragEnd = {
                                seekPreviewMs?.let { vm.seekTo(it); tick++ }
                                seekPreviewMs = null
                            },
                            onDragCancel = { seekPreviewMs = null },
                        )
                    }
                    // 竖直拖动：左半=亮度、右半=音量（点播/直播通用）
                    .pointerInput(Unit) {
                        detectVerticalDragGestures(
                            onDragStart = { off ->
                                val isBrightness = off.x < size.width / 2f
                                if (isBrightness) {
                                    vbKind = "brightness"
                                    vbValue = runCatching {
                                        val cur = (dragCtx as? Activity)?.window?.attributes?.screenBrightness ?: -1f
                                        if (cur < 0f) 0.5f else cur
                                    }.getOrDefault(0.5f)
                                } else {
                                    vbKind = "volume"
                                    vbValue = runCatching {
                                        val am = dragCtx.getSystemService(android.content.Context.AUDIO_SERVICE)
                                            as? android.media.AudioManager
                                        val max = am?.getStreamMaxVolume(android.media.AudioManager.STREAM_MUSIC) ?: 15
                                        val cur = am?.getStreamVolume(android.media.AudioManager.STREAM_MUSIC) ?: 0
                                        cur / max.toFloat()
                                    }.getOrDefault(0.5f)
                                }
                            },
                            onVerticalDrag = { change, dragAmount ->
                                change.consume()
                                if (vbKind == null) return@detectVerticalDragGestures
                                // 上滑增加；约一屏行程 = 全量程
                                vbValue = (vbValue - dragAmount / size.height.toFloat() * 1.2f)
                                    .coerceIn(0.02f, 1f)
                                when (vbKind) {
                                    "brightness" -> runCatching {
                                        val a = dragCtx as? Activity
                                        a?.window?.attributes = a?.window?.attributes?.apply {
                                            screenBrightness = vbValue
                                        }
                                    }
                                    "volume" -> runCatching {
                                        val am = dragCtx.getSystemService(android.content.Context.AUDIO_SERVICE)
                                            as? android.media.AudioManager ?: return@runCatching
                                        val max = am.getStreamMaxVolume(android.media.AudioManager.STREAM_MUSIC)
                                        am.setStreamVolume(
                                            android.media.AudioManager.STREAM_MUSIC,
                                            (vbValue * max).roundToInt(), 0,
                                        )
                                    }
                                }
                            },
                            onDragEnd = { vbKind = null },
                            onDragCancel = { vbKind = null },
                        )
                    },
            )

            // 手势反馈浮层：滑动快进预览 / 亮度 / 音量
            GestureIndicator(
                seekPreviewMs = seekPreviewMs,
                seekDir = seekDir,
                vbKind = vbKind,
                vbValue = vbValue,
            )
        }

        // 影院 scrim（控制条可见或未起播时淡入；tvOS 径向压暗 + 底部渐黑，TV 版 1:1）
        val scrimAlpha by animateFloatAsState(
            targetValue = if ((ui.controlsVisible && !ui.isLive) || (ui.playState == EnginePlayState.IDLE && ui.fatalMsg == null && !ui.isLive)) 1f else 0f,
            animationSpec = tween(450),
            label = "scrimAlpha",
        )
        if (scrimAlpha > 0f) {
            Box(Modifier.fillMaxSize().alpha(scrimAlpha)) {
                BoxWithConstraints(Modifier.fillMaxSize()) {
                    val density = LocalDensity.current
                    val w = with(density) { maxWidth.toPx() }
                    val h = with(density) { maxHeight.toPx() }
                    Box(
                        Modifier
                            .fillMaxSize()
                            .background(
                                Brush.radialGradient(
                                    colors = listOf(Color(0x33000000), Color.Transparent),
                                    center = Offset(w / 2f, h * 0.35f),
                                    radius = maxOf(w, h) * 0.75f,
                                ),
                            ),
                    )
                }
                Box(
                    Modifier
                        .fillMaxWidth()
                        .fillMaxHeight(0.3f)
                        .align(Alignment.BottomCenter)
                        .background(
                            Brush.verticalGradient(
                                0f to Color.Transparent,
                                0.55f to Color(0x66000000),
                                1f to Color(0xCC000000),
                            ),
                        ),
                )
            }
        }

        // 缓冲指示
        if (ui.playState == EnginePlayState.BUFFERING && ui.fatalMsg == null) {
            AppleLoading(modifier = Modifier.align(Alignment.Center), size = 56.dp)
        }

        // 错误兜底（防线三，与 TV 版同文案同视觉）
        ui.fatalMsg?.let { msg ->
            ErrorWidget(
                message = msg,
                onRetry = { vm.retry() },
                onBack = { nav.popBackStack() },
            )
        }

        if (ui.isLive) {
            // ---- 足球直播（需求 足球#14）：无任何菜单、无比分板；唯一控件=信号源选择条 ----
            LiveSourcePicker(ui, vm, Modifier.align(Alignment.BottomCenter), onTick = { tick++ })
        } else {
            // ---- 影视：tvOS 播放器（需求 影视#3，视觉 TV 版 1:1）----
            VodPlayerOverlay(
                ui = ui,
                vm = vm,
                onTick = { tick++ },
            )
        }

        // 面板：更多（影片信息+选集[仅非电影]+换源+倍速+画幅；右侧悬浮栏，TV 版 1:1）
        if (ui.panel == Panel.MORE) {
            MorePanel(
                ui = ui,
                vm = vm,
                onDismiss = { vm.setPanel(Panel.NONE) },
                // v1.19：VM 内直切换集（不走路由堆叠 VM/引擎实例，见 switchEpisode）
                onPickEpisode = { i ->
                    vm.setPanel(Panel.NONE)
                    vm.switchEpisode(i)
                },
            )
        }

        // 总体#6：返回按钮（控制浮层可见时左上角；与手势/系统返回同语义）
        if (((ui.controlsVisible && !ui.isLive) || ui.srcPickerVisible) && ui.fatalMsg == null) {
            com.qiubo.optimaltv.ui.components.MobileBackButton(
                onBack = { nav.popBackStack() },
                modifier = Modifier.align(Alignment.TopStart),
            )
        }

        // BACK 分层处理
        BackHandler { onBackPressed() }

        // 返回提示（需求 足球#5；需求 影视#3：统一贴屏幕最下方）
        OtvHint(backHint.value)
    }

    // 需求12⑥⑦：竖屏操作区（视频下方常驻；横屏全屏播放时不渲染）
    if (portrait && ui.fatalMsg == null) {
        PortraitPlayerPanel(ui = ui, vm = vm)
    }
    } // Column
}

/** 竖屏播放器操作区（需求12⑥⑦）：标题/meta + 进度条（点按 seek）+ 播放/全屏/更多。
 *  全屏 = 请求横屏（沉浸全屏播放器即系统级全屏），退出本页自动恢复方向跟随。 */
@Composable
private fun PortraitPlayerPanel(ui: PlayerUiState, vm: PlayerViewModel) {
    val activity = LocalContext.current as Activity
    PortraitPlayerControls(
        title = ui.title,
        meta = ui.meta,
        positionMs = ui.positionMs,
        durationMs = ui.durationMs,
        isLive = ui.isLive,
        isPlaying = ui.isPlaying,
        onSeek = vm::seekTo,
        onTogglePlay = vm::togglePlay,
        onFullscreen = {
            activity.requestedOrientation = android.content.pm.ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
        },
        onMore = { vm.setPanel(Panel.MORE) },
    )
}

/**
 * 手势反馈浮层（影视#1 v1.19）：滑动快进时中央显示目标时间；竖滑调节时
 * 中央显示亮度/音量标签与进度条。手势结束自动消失。
 *
 * 2026-09-17 用户要求：快进/快退那个图标改 Apple 极简风、去掉深色底——现在是
 * SF Symbols `goforward`/`gobackward` 同款白描边圆箭头，只有一层柔和文字投影。
 */
@Composable
private fun GestureIndicator(
    seekPreviewMs: Long?,
    seekDir: Int,
    vbKind: String?,
    vbValue: Float,
) {
    if (seekPreviewMs == null && vbKind == null) return
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        if (seekPreviewMs != null) {
            /* 无底色：只用白色描边图标 + 时间，靠投影保证在浅色画面上也能看清 */
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                SeekDirectionGlyph(forward = seekDir >= 0, size = 88.dp)
                Spacer(Modifier.height(16.dp))
                Text(
                    formatTime(seekPreviewMs),
                    color = Color.White, fontSize = 32.sp, fontWeight = FontWeight.SemiBold,
                    style = with(LocalDensity.current) {
                        LocalTextStyle.current.copy(
                            shadow = Shadow(color = Color(0x99000000), blurRadius = 20f, offset = Offset(0f, 3f)),
                        )
                    },
                )
            }
        } else if (vbKind != null) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                modifier = Modifier
                    .background(Color(0xB3000000), RoundedCornerShape(18.dp))
                    .padding(horizontal = 28.dp, vertical = 20.dp),
            ) {
                Text(
                    if (vbKind == "brightness") "亮度" else "音量",
                    color = Color.White, fontSize = 16.sp, fontWeight = FontWeight.Medium,
                    modifier = Modifier.padding(bottom = 10.dp),
                )
                Box(
                    Modifier
                        .width(180.dp)
                        .height(5.dp)
                        .background(Color(0x4DFFFFFF), RoundedCornerShape(3.dp)),
                ) {
                    Box(
                        Modifier
                            .fillMaxHeight()
                            .fillMaxWidth(vbValue.coerceIn(0f, 1f))
                            .background(Color.White, RoundedCornerShape(3.dp)),
                    )
                }
            }
        }
    }
}

/**
 * Apple 风格「快进 / 快退」圆箭头（SF Symbols `goforward` / `gobackward` 同款）：
 * 白描边圆环缺口开在正上方，收口处一个实心箭头沿圆周切向指向前进方向，完全无底色。
 *
 * 屏幕坐标角度：0°=右、90°=下、270°=上。forward 走顺时针（330°→260°），
 * backward 水平镜像（210°→280°），两者箭头都落在正上方、分别朝右 / 朝左。
 */
@Composable
private fun SeekDirectionGlyph(forward: Boolean, size: Dp) {
    Canvas(Modifier.size(size)) {
        val s = this.size.minDimension
        val stroke = s * 0.075f
        val radius = s / 2f - stroke / 2f - s * 0.05f
        val center = Offset(this.size.width / 2f, this.size.height / 2f)

        fun at(deg: Float, r: Float = radius): Offset {
            val rad = Math.toRadians(deg.toDouble())
            return Offset(center.x + r * kotlin.math.cos(rad).toFloat(),
                          center.y + r * kotlin.math.sin(rad).toFloat())
        }

        val dir = if (forward) 1f else -1f
        drawArc(
            color = Color.White,
            startAngle = if (forward) 330f else 210f,
            sweepAngle = 290f * dir,
            useCenter = false,
            topLeft = Offset(center.x - radius, center.y - radius),
            size = androidx.compose.ui.geometry.Size(radius * 2f, radius * 2f),
            style = Stroke(width = stroke, cap = StrokeCap.Round),
        )
        // 箭头：底边钉在圆弧收口处（270° - 10°·dir），尖端再往前 24° 沿切向
        val arcEnd = 270f - 10f * dir
        val tip = at(arcEnd + 24f * dir)
        val baseA = at(arcEnd, radius + stroke * 1.7f)
        val baseB = at(arcEnd, radius - stroke * 1.7f)
        drawPath(
            Path().apply {
                moveTo(tip.x, tip.y)
                lineTo(baseA.x, baseA.y)
                lineTo(baseB.x, baseB.y)
                close()
            },
            Color.White,
        )
    }
}

/**
 * 直播信号源选择条（需求 足球#14：直播播放器唯一控件；视觉 TV 版 1:1）：
 * 单击画面呼出 → 点 chip 即时换线；BACK 或 5s 无操作隐藏。默认源 = bb。
 */
@Composable
private fun LiveSourcePicker(
    ui: PlayerUiState,
    vm: PlayerViewModel,
    modifier: Modifier,
    onTick: () -> Unit,
) {
    val ps = rememberUiScale()
    if (!ui.srcPickerVisible) return

    Column(
        modifier
            .padding(bottom = 56f.sx(ps))
            .background(Color(0xB3111114), RoundedCornerShape(20f.sx(ps)))
            .padding(horizontal = 26f.sx(ps), vertical = 20f.sx(ps)),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            "信号源",
            color = Color(0x99FFFFFF), fontSize = 20f.sxs(ps),
            modifier = Modifier.padding(bottom = 12f.sx(ps)),
        )
        Row(horizontalArrangement = Arrangement.spacedBy(14f.sx(ps))) {
            ui.lines.forEachIndexed { idx, line ->
                val active = idx == ui.activeLine
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .clip(RoundedCornerShape(12f.sx(ps)))
                        .tapCard { vm.switchLine(idx); onTick() }
                        .background(
                            if (active) Color(0x59FFFFFF) else Color(0x24FFFFFF),
                            RoundedCornerShape(12f.sx(ps)),
                        )
                        .padding(horizontal = 24f.sx(ps), vertical = 12f.sx(ps)),
                ) {
                    Text(
                        line.label,
                        color = if (active) OtvColors.Bg else Color.White,
                        fontSize = 25f.sxs(ps),
                        fontWeight = if (active) FontWeight.Bold else FontWeight.Normal,
                        maxLines = 1,
                    )
                }
            }
        }
    }
}

/**
 * tvOS 风格影视播放器浮层（视觉 TV 版 1:1，交互触控）：
 * 右上更多钮 / 底部标题+meta → 进度行（播放暂停钮 - 已播 - 轨道(可拖动) - 剩余）；
 * 双击画面左右 1/3 = ±10s（外层手势层处理）。
 */
@Composable
private fun VodPlayerOverlay(
    ui: PlayerUiState,
    vm: PlayerViewModel,
    onTick: () -> Unit,
) {
    val ps = rememberUiScale()
    // v1.21 投屏：进度镜像改渲染器上报值；投屏中横幅。
    // 2026-09-17 用户要求：投屏入口从画面左上角挪到「更多」面板里（放在「画幅」下面），
    // 可用设备直接列在入口下方，不再弹独立底部 sheet。
    val castOn = ui.castDevice != null
    val posMs = if (castOn) ui.castPositionMs else ui.positionMs
    val durMs = if (castOn) (ui.castDurationMs.takeIf { it > 0 } ?: ui.durationMs) else ui.durationMs
    Box(Modifier.fillMaxSize()) {
        if (castOn) {
            // 投屏中横幅（2026-09-05 视觉稿定稿：蓝调胶囊——绿点 + 投屏中 + 设备名；点击断开回本地续播）
            // 2026-09-19 用户需求：胶囊移到画面正上方居中（原左上角）
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .align(Alignment.TopCenter)
                    .padding(top = 90f.sx(ps))
                    .clip(RoundedCornerShape(50))
                    .background(AccentBlue.copy(alpha = 0.28f))
                    .border(1.dp, AccentBlue.copy(alpha = 0.55f), RoundedCornerShape(50))
                    .clickable { vm.stopCast(resumeLocal = true) }
                    .padding(horizontal = 26f.sx(ps), vertical = 13f.sx(ps)),
            ) {
                Box(Modifier.size(12f.sx(ps)).background(Color(0xFF30D158), CircleShape))
                Spacer(Modifier.width(12f.sx(ps)))
                Text("投屏中", color = Color.White, fontSize = 26f.sxs(ps), fontWeight = FontWeight.SemiBold, maxLines = 1)
                Spacer(Modifier.width(14f.sx(ps)))
                Text(ui.castDevice?.name ?: "", color = OtvColors.White70, fontSize = 24f.sxs(ps), maxLines = 1, overflow = TextOverflow.Ellipsis)
                Spacer(Modifier.width(14f.sx(ps)))
                Text("点按断开", color = OtvColors.White50, fontSize = 22f.sxs(ps), maxLines = 1)
            }
        }
    Box(Modifier.fillMaxSize()) {
        if (ui.controlsVisible && ui.fatalMsg == null) {
            // 「更多」钮（需求 影视#5）：右上角，TV 版 1:1 位置与尺寸
            Box(
                Modifier
                    .align(Alignment.TopEnd)
                    .padding(end = 80f.sx(ps), top = 90f.sx(ps)),
            ) {
                CircleButton(size = 52.dp, onClick = { vm.setPanel(Panel.MORE); onTick() }) { c ->
                    Canvas(Modifier.size(30.dp)) {
                        val r = 2.6f
                        val gap = 10f
                        val cy = size.height / 2f
                        listOf(cy - gap, cy, cy + gap).forEach { y ->
                            drawCircle(c, radius = r, center = Offset(size.width / 2f, y))
                        }
                    }
                }
            }

            // 底部：标题+meta（需求 影视#4）→ 进度行（播放暂停钮 - 已播 - 轨道 - 剩余）
            Column(
                Modifier
                    .align(Alignment.BottomCenter)
                    .fillMaxWidth()
                    .padding(horizontal = 80f.sx(ps), vertical = 40f.sx(ps)),
            ) {
                // 标题 + meta（tvOS：需求 影视#9 字号缩小；多集时标题带当前集数）
                val displayTitle = if (ui.epCount > 1) "${ui.title} 第${ui.epIndex + 1}集" else ui.title
                Text(
                    displayTitle.ifBlank { "正在播放" },
                    color = OtvColors.White, fontSize = 52f.sxs(ps), fontWeight = FontWeight.Bold,
                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                    style = with(LocalDensity.current) {
                        LocalTextStyle.current.copy(
                            shadow = Shadow(color = Color(0x80000000), blurRadius = 24f.sx(ps).toPx(), offset = Offset(0f, 4f.sx(ps).toPx())),
                        )
                    },
                )
                if (ui.meta.isNotBlank()) {
                    Spacer(Modifier.height(10f.sx(ps)))
                    Text(
                        ui.meta, color = OtvColors.White.copy(alpha = 0.85f), fontSize = 23f.sxs(ps),
                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                    )
                }
                Spacer(Modifier.height(26f.sx(ps)))
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    // 播放/暂停（进度条前面）
                    CircleButton(size = 30.dp, onClick = { vm.togglePlay(); onTick() }) { c ->
                        PlayPauseIcon(playing = ui.isPlaying, iconColor = c, iconSize = 13.dp)
                    }
                    Spacer(Modifier.width(24f.sx(ps)))
                    Text(formatTime(posMs), color = Color(0xE6FFFFFF), fontSize = 24f.sxs(ps), fontWeight = FontWeight.SemiBold)
                    // 轨道（视觉 TV 版 1:1；触控：拖动/点按 seek——对应 TV 版聚焦左右键 ±30s 的触控等价）
                    var scrubFrac by remember { mutableStateOf<Float?>(null) }
                    val frac = scrubFrac
                        ?: if (durMs > 0) posMs.toFloat() / durMs else 0f
                    Box(
                        Modifier
                            .weight(1f)
                            .padding(horizontal = 28.dp)
                            .pointerInput(durMs) {
                                detectTapGestures(
                                    onTap = { off ->
                                        if (durMs > 0) vm.seekTo((dragFracOf(off.x, size.width) * durMs).toLong())
                                        onTick()
                                    },
                                )
                            }
                            .pointerInput(durMs) {
                                detectHorizontalDragGestures(
                                    onHorizontalDrag = { change, _ ->
                                        change.consume()
                                        if (durMs > 0) scrubFrac = dragFracOf(change.position.x, size.width)
                                    },
                                    onDragEnd = {
                                        scrubFrac?.let { vm.seekTo((it * durMs).toLong()) }
                                        scrubFrac = null
                                        onTick()
                                    },
                                )
                            }
                            .padding(vertical = 10.dp),
                    ) {
                        Box(
                            Modifier
                                .fillMaxWidth()
                                .height(6.dp)
                                .background(Color(0x4DFFFFFF), RoundedCornerShape(5.dp)),
                        ) {
                            Box(
                                Modifier
                                    .fillMaxHeight()
                                    .fillMaxWidth(frac.coerceIn(0.005f, 1f)),
                            ) {
                                Box(Modifier.fillMaxSize().background(Color.White, RoundedCornerShape(5.dp)))
                                Box(
                                    Modifier
                                        .align(Alignment.CenterEnd)
                                        .size(14.dp)
                                        .background(Color.White, CircleShape),
                                )
                            }
                        }
                    }
                    Text(
                        "-" + formatTime((durMs - posMs).coerceAtLeast(0)),
                        color = Color(0xE6FFFFFF), fontSize = 24f.sxs(ps), fontWeight = FontWeight.SemiBold,
                    )
                }
            }
        } else if (ui.playState == EnginePlayState.IDLE && ui.fatalMsg == null) {
            // 未起播态：仅中央大播放钮
            Box(Modifier.align(Alignment.Center)) {
                CircleButton(size = 96.dp, onClick = { vm.togglePlay(); onTick() }) { c ->
                    PlayPauseIcon(playing = true, iconColor = c, iconSize = 40.dp)
                }
            }
        }
        }
    }
}

/**
 * 右侧悬浮栏「更多」（TV 版 1:1）：影片信息 + 选集（仅电视剧/动漫/短剧，电影不显示）
 * + 换源 + 倍速 + 画幅。触控点按操作。
 */
@Composable
private fun MorePanel(
    ui: PlayerUiState,
    vm: PlayerViewModel,
    onDismiss: () -> Unit,
    onPickEpisode: (Int) -> Unit,
) {
    // 打开面板即扫一次局域网，设备自动列在「投屏」下方（用户要求：不用再点一次才出列表）
    LaunchedEffect(Unit) { vm.scanCastDevices() }
    Box(Modifier.fillMaxSize()) {
        // 全屏暗区：视觉压暗视频；点暗区关闭浮窗（对应 TV 版 BACK 关闭）
        Box(
            Modifier
                .fillMaxSize()
                .background(Color(0x66000000))
                .pointerInput(Unit) { detectTapGestures { onDismiss() } },
        )
        Column(
            Modifier
                .align(Alignment.CenterEnd)
                .width(with(androidx.compose.ui.platform.LocalConfiguration.current) {
    if (orientation == android.content.res.Configuration.ORIENTATION_PORTRAIT)
        (screenWidthDp * 0.92f).dp else 560.dp
})
                .fillMaxHeight()
                .background(
                    Color(0xE9121216),
                    RoundedCornerShape(topStart = 24.dp, bottomStart = 24.dp),
                )
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 34.dp, vertical = 28.dp),
        ) {
            // 影片信息
            Row(verticalAlignment = Alignment.Top) {
                if (ui.posterUrl.isNotBlank()) {
                    AsyncImage(
                        model = ui.posterUrl,
                        contentDescription = ui.title,
                        contentScale = ContentScale.Crop,
                        modifier = Modifier
                            .width(96.dp)
                            .height(136.dp)
                            .clip(RoundedCornerShape(10.dp)),
                    )
                    Spacer(Modifier.width(18.dp))
                }
                Column(Modifier.weight(1f)) {
                    Text(
                        ui.title.ifBlank { "影片" },
                        color = TextPrimary, style = MaterialTheme.typography.titleMedium,
                        maxLines = 2, overflow = TextOverflow.Ellipsis,
                    )
                    if (ui.meta.isNotBlank()) {
                        Spacer(Modifier.height(6.dp))
                        Text(ui.meta, color = TextPrimary.copy(alpha = 0.6f), style = MaterialTheme.typography.bodySmall, maxLines = 2)
                    }
                    if (ui.lines.isNotEmpty()) {
                        Spacer(Modifier.height(4.dp))
                        Text(
                            "线路${ui.activeLine + 1}/${ui.lines.size} · ${ui.engineName}",
                            color = TextPrimary.copy(alpha = 0.5f), style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
            if (ui.desc.isNotBlank()) {
                Spacer(Modifier.height(14.dp))
                Text(
                    ui.desc, color = TextPrimary.copy(alpha = 0.7f), style = MaterialTheme.typography.bodySmall,
                    maxLines = 4, overflow = TextOverflow.Ellipsis,
                )
            }
            Spacer(Modifier.height(20.dp))
            // 选集（需求⑨修订：电影不显示选集区，电视剧/动漫/短剧保留；分页每页 20 集 4 列）
            if (!ui.isMovie && ui.epCount > 1) {
                SectionLabel("选择集数")
                val pageSize = EpisodeMenuPolicy.RANGE_SIZE
                val pageCount = (ui.epCount + pageSize - 1) / pageSize
                var page by remember(ui.epIndex / pageSize) { mutableIntStateOf(ui.epIndex / pageSize) }
                if (EpisodeMenuPolicy.hasRanges(ui.epCount)) Row(
                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                    modifier = Modifier
                        .padding(bottom = 10.dp)
                        .horizontalScroll(rememberScrollState()),
                ) {
                    for (p in 0 until pageCount) {
                        val from = p * pageSize + 1
                        val to = minOf((p + 1) * pageSize, ui.epCount)
                        Box(
                            contentAlignment = Alignment.Center,
                            modifier = Modifier
                                .clip(RoundedCornerShape(10.dp))
                                .tapCard { page = p }
                                .background(
                                    if (p == page) AccentBlue else Color(0x24FFFFFF),
                                    RoundedCornerShape(10.dp),
                                )
                                .padding(horizontal = 14.dp, vertical = 8.dp),
                        ) {
                            Text("第$from-${to}集", color = Color.White, style = MaterialTheme.typography.labelMedium)
                        }
                    }
                }
                val pageStart = page * pageSize
                val pageEps = pageStart until minOf(pageStart + pageSize, ui.epCount)
                LazyVerticalGrid(
                    columns = GridCells.Fixed(4),
                    modifier = Modifier.height(((pageEps.count() + 3) / 4 * 52).dp),
                ) {
                    items(pageEps.toList()) { i ->
                        Box(
                            contentAlignment = Alignment.Center,
                            modifier = Modifier
                                .padding(4.dp)
                                .clip(RoundedCornerShape(10.dp))
                                .tapCard { onPickEpisode(i) }
                                .background(
                                    if (i == ui.epIndex) AccentBlue else Color(0x24FFFFFF),
                                    RoundedCornerShape(10.dp),
                                )
                                .padding(vertical = 10.dp),
                        ) {
                            Text(
                                EpisodeMenuPolicy.episodeLabel(i),
                                color = Color.White, style = MaterialTheme.typography.labelMedium,
                                maxLines = 1, overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                }
                Spacer(Modifier.height(18.dp))
            }
            // 换源（horizontalScroll：14+ 条线路不裁切）
            if (ui.lines.size > 1) {
                SectionLabel("换源线路")
                Row(
                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                    modifier = Modifier.horizontalScroll(rememberScrollState()),
                ) {
                    ui.lines.forEachIndexed { idx, line ->
                        Box(
                            modifier = Modifier
                                .clip(RoundedCornerShape(10.dp))
                                .tapCard { vm.switchLine(idx) }
                                .background(
                                    if (idx == ui.activeLine) AccentBlue else Color(0x24FFFFFF),
                                    RoundedCornerShape(10.dp),
                                )
                                .padding(horizontal = 14.dp, vertical = 9.dp),
                        ) {
                            Text(line.label, color = Color.White, style = MaterialTheme.typography.labelMedium, maxLines = 1)
                        }
                    }
                }
                Spacer(Modifier.height(18.dp))
            }
            // 倍速
            SectionLabel("倍速")
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                PlayerViewModel.SPEED_STEPS.forEach { sp ->
                    val active = kotlin.math.abs(sp - ui.speed) < 0.01f
                    Box(
                        contentAlignment = Alignment.Center,
                        modifier = Modifier
                            .clip(RoundedCornerShape(10.dp))
                            .tapCard { vm.setSpeed(sp) }
                            .background(
                                if (active) AccentBlue else Color(0x24FFFFFF),
                                RoundedCornerShape(10.dp),
                            )
                            .padding(horizontal = 12.dp, vertical = 9.dp),
                    ) {
                        Text("%.1fx".format(sp), color = Color.White, style = MaterialTheme.typography.labelMedium)
                    }
                }
            }
            Spacer(Modifier.height(18.dp))
            // 画幅
            SectionLabel("画幅")
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                com.qiubo.optimaltv.data.prefs.AspectMode.entries.forEach { mode ->
                    val active = mode.ordinal == ui.aspectOrdinal
                    Box(
                        contentAlignment = Alignment.Center,
                        modifier = Modifier
                            .clip(RoundedCornerShape(10.dp))
                            .tapCard { vm.setAspect(mode.ordinal) }
                            .background(
                                if (active) AccentBlue else Color(0x24FFFFFF),
                                RoundedCornerShape(10.dp),
                            )
                            .padding(horizontal = 12.dp, vertical = 9.dp),
                    ) {
                        Text(mode.label, color = Color.White, style = MaterialTheme.typography.labelMedium)
                    }
                }
            }
            Spacer(Modifier.height(18.dp))
            // 投屏（2026-09-17 用户要求）：入口挪到「画幅」下面，可投屏设备自动列在下方，
            // 不再需要先点一次按钮才出列表、也不弹独立底部 sheet。
            SectionLabel("投屏")
            val otherCastDevices = ui.castDevices.filter { it.uuid != ui.castDevice?.uuid }
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                // 投屏中设备：置顶显示断开
                ui.castDevice?.let { dev ->
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(12.dp))
                            .background(Color(0x240A84FF))
                            .clickable { vm.stopCast(resumeLocal = true) }
                            .padding(horizontal = 18.dp, vertical = 14.dp),
                    ) {
                        CastGlyph(24.dp, AccentBlue)
                        Spacer(Modifier.width(12.dp))
                        Text(
                            "正在投屏 · ${dev.name}", color = AccentBlue, modifier = Modifier.weight(1f),
                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                        )
                        Text("断开", color = TextPrimary.copy(alpha = 0.7f), style = MaterialTheme.typography.labelMedium)
                    }
                }
                otherCastDevices.forEach { dev ->
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(12.dp))
                            .background(Color(0x24FFFFFF))
                            .clickable { vm.startCast(dev) }
                            .padding(horizontal = 18.dp, vertical = 14.dp),
                    ) {
                        CastGlyph(24.dp, TextPrimary.copy(alpha = 0.7f))
                        Spacer(Modifier.width(12.dp))
                        Text(
                            dev.name, color = TextPrimary, modifier = Modifier.weight(1f),
                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                        )
                        Text("投屏", color = Color.White, style = MaterialTheme.typography.labelMedium)
                    }
                }
                if (otherCastDevices.isEmpty() && ui.castDevice == null) {
                    val castHint = ui.castError
                    Text(
                        when {
                            ui.castScanning -> "正在扫描局域网…"
                            castHint != null -> castHint
                            else -> "未发现设备（电视需与本机同一 Wi-Fi）"
                        },
                        color = if (castHint != null && !ui.castScanning) Color(0xFFFFB84D)
                                else TextPrimary.copy(alpha = 0.55f),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                Row(
                    horizontalArrangement = Arrangement.Center,
                    modifier = Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(12.dp))
                        .border(0.5.dp, Color(0x33FFFFFF), RoundedCornerShape(12.dp))
                        .clickable { vm.scanCastDevices() }
                        .padding(horizontal = 18.dp, vertical = 12.dp),
                ) {
                    Text("重新扫描", color = TextPrimary.copy(alpha = 0.8f), style = MaterialTheme.typography.labelMedium)
                }
            }
            Spacer(Modifier.height(28.dp))
        }
    }
}

@Composable
private fun SectionLabel(text: String) {
    Text(
        text, color = TextPrimary, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold,
        modifier = Modifier.padding(bottom = 10.dp),
    )
}

/** 标准 DLNA 投屏图标（Cast 风格：双波纹 + 实心屏角）——投屏胶囊/状态卡共用 */
@Composable
private fun CastGlyph(size: Dp, color: Color) {
    Canvas(Modifier.size(size)) {
        val s = this.size.width
        drawArc(
            color = color,
            startAngle = 200f, sweepAngle = 100f, useCenter = false,
            topLeft = Offset(s * 0.08f, s * 0.30f),
            size = androidx.compose.ui.geometry.Size(s * 0.84f, s * 0.84f),
            style = Stroke(width = s * 0.09f, cap = StrokeCap.Round),
        )
        drawArc(
            color = color,
            startAngle = 200f, sweepAngle = 100f, useCenter = false,
            topLeft = Offset(s * 0.24f, s * 0.46f),
            size = androidx.compose.ui.geometry.Size(s * 0.52f, s * 0.52f),
            style = Stroke(width = s * 0.09f, cap = StrokeCap.Round),
        )
        drawRoundRect(
            color = color,
            topLeft = Offset(s * 0.42f, s * 0.66f),
            size = androidx.compose.ui.geometry.Size(s * 0.18f, s * 0.24f),
            cornerRadius = CornerRadius(s * 0.05f),
        )
    }
}

/** 毛玻璃圆形按钮基座（TV 版视觉：深色毛玻璃底 + 细描边；触控按压反馈） */
@Composable
private fun CircleButton(
    size: Dp = 56.dp,
    onClick: () -> Unit,
    content: @Composable (iconColor: Color) -> Unit,
) {
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .size(size)
            .clip(CircleShape)
            .tapCard(onClick)
            .background(Color(0x66141414), CircleShape)
            .border(1.dp, Color(0x1FFFFFFF), CircleShape),
    ) {
        content(Color.White)
    }
}

/** 播放三角 / 暂停双竖条 */
@Composable
private fun PlayPauseIcon(playing: Boolean, iconColor: Color, iconSize: Dp) {
    if (playing) {
        Canvas(Modifier.size(iconSize)) {
            val w = this.size.width
            val h = this.size.height
            val barW = w * 0.24f
            val gap = w * 0.14f
            val r = barW / 2f
            val y0 = h * 0.18f
            val y1 = h * 0.82f
            val cx = w / 2f
            drawRoundRect(
                iconColor,
                topLeft = Offset(cx - gap / 2f - barW, y0),
                size = androidx.compose.ui.geometry.Size(barW, y1 - y0),
                cornerRadius = androidx.compose.ui.geometry.CornerRadius(r, r),
            )
            drawRoundRect(
                iconColor,
                topLeft = Offset(cx + gap / 2f, y0),
                size = androidx.compose.ui.geometry.Size(barW, y1 - y0),
                cornerRadius = androidx.compose.ui.geometry.CornerRadius(r, r),
            )
        }
    } else {
        Canvas(Modifier.size(iconSize)) {
            val w = this.size.width
            val h = this.size.height
            val path = Path().apply {
                moveTo(w * 0.30f, h * 0.18f)
                lineTo(w * 0.82f, h * 0.50f)
                lineTo(w * 0.30f, h * 0.82f)
                close()
            }
            drawPath(path, iconColor)
        }
    }
}

@Composable
private fun ErrorWidget(
    message: String,
    onRetry: () -> Unit,
    onBack: () -> Unit,
) {
    Box(
        Modifier
            .fillMaxSize()
            .background(Color(0xCC000000)),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(18.dp),
            modifier = Modifier
                .width(720.dp)
                .background(Color(0xFF1C1C22), RoundedCornerShape(20.dp))
                .padding(40.dp),
        ) {
            Text("无法播放", style = MaterialTheme.typography.titleLarge, color = TextPrimary)
            Text(
                message,
                style = MaterialTheme.typography.bodyMedium,
                color = Color(0xFFB9C0CC),
                maxLines = 4,
            )
            Text("已尝试全部线路。可重试或返回选择其他影片。",
                style = MaterialTheme.typography.bodyMedium, color = Color(0xFFB9C0CC))
            Row(horizontalArrangement = Arrangement.spacedBy(20.dp)) {
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .clip(RoundedCornerShape(12.dp))
                        .tapCard(onRetry)
                        .background(AccentBlue, RoundedCornerShape(12.dp))
                        .padding(horizontal = 34.dp, vertical = 12.dp),
                ) { Text("重试", color = Color.White, style = MaterialTheme.typography.labelLarge) }
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .clip(RoundedCornerShape(12.dp))
                        .tapCard(onBack)
                        .background(Color(0x2EFFFFFF), RoundedCornerShape(12.dp))
                        .padding(horizontal = 34.dp, vertical = 12.dp),
                ) { Text("返回", color = Color.White, style = MaterialTheme.typography.labelLarge) }
            }
        }
    }
}

/** 进度条拖动/点按位置 → 播放进度比例（0..1） */
private fun dragFracOf(x: Float, widthPx: Int): Float =
    if (widthPx <= 0) 0f else (x / widthPx.toFloat()).coerceIn(0f, 1f)

/** 画幅模式 → 渲染容器约束（与 TV 版同款） */
private fun aspectConstraint(ord: Int): Modifier =
    when (val m = com.qiubo.optimaltv.data.prefs.AspectMode.of(ord)) {
        com.qiubo.optimaltv.data.prefs.AspectMode.R16_9 -> Modifier.fillMaxSize().aspectRatio(16f / 9f)
        com.qiubo.optimaltv.data.prefs.AspectMode.R4_3 -> Modifier.fillMaxSize().aspectRatio(4f / 3f)
        else -> Modifier.fillMaxSize().then(m.ratio?.let { Modifier.aspectRatio(it) } ?: Modifier)
    }

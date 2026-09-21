package com.qiubo.optimaltv.ui.player

import android.app.Activity
import android.view.KeyEvent
import android.view.WindowManager
import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
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
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.Shadow
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.foundation.focusable
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
import androidx.compose.ui.layout.ContentScale
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
import com.qiubo.optimaltv.ui.components.InitialFocusEffect
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.ProvideNavScrolls
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.components.navSink
import com.qiubo.optimaltv.ui.components.scrollNavContainer
import com.qiubo.optimaltv.ui.detail.simpleFactory
import com.qiubo.optimaltv.ui.theme.AccentBlue
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sxs
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.TextPrimary
import java.net.URLDecoder

/**
 * 播放页（2026-08-28 复刻 tvOS 播放器，需求 影视#3 / 足球#13/14）：
 * - 影视：OK 呼出菜单（标题+meta / 中央三键组 / 进度条 / 更多钮）；BACK 隐藏菜单；
 *   菜单隐藏时两次返回退出（首次提示）；左右=±10s；更多钮或菜单键 → 右侧悬浮栏（影片信息+选集+换源+倍速+画幅）
 * - 足球直播（需求 足球#14）：无任何菜单、无比分板；唯一控件=信号源选择条
 *   （OK/菜单键呼出，左右移动、OK 切换，BACK/5s 自动隐藏）；默认源=bb（原版足球直播2）；
 *   两次返回退出（首次提示）
 * - KEEP_SCREEN_ON / 断点续播 / 看门狗换线（VM 承担）
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

    // KEEP_SCREEN_ON：投影仪防息屏
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

    var tick by remember { mutableIntStateOf(0) }   // 控制条/信号源条自动隐藏计时基准
    LaunchedEffect(ui.controlsVisible, ui.panel, ui.srcPickerVisible, tick) {
        if (ui.panel == Panel.NONE && ui.fatalMsg == null) {
            kotlinx.coroutines.delay(5_000)
            if (ui.srcPickerVisible) vm.hideSrcPicker()
            else if (ui.controlsVisible) vm.hideControls()
        }
    }

    // 返回语义（需求）：面板开 → 关面板；信号源条开（直播）→ 收起；菜单开 → 隐藏菜单；
    // 菜单隐藏 → 2.5s 内两次返回退出（首次提示）
    val backHint = remember { mutableStateOf<String?>(null) }
    var lastBackAt by remember { mutableLongStateOf(0L) }
    LaunchedEffect(backHint.value) {
        if (backHint.value != null) { kotlinx.coroutines.delay(2500); backHint.value = null }
    }
    fun onBackPressed() {
        com.qiubo.optimaltv.OtvLog.i("player onBackPressed: panel=${ui.panel} src=${ui.srcPickerVisible} ctl=${ui.controlsVisible} fatal=${ui.fatalMsg != null} hint=${backHint.value != null}")
        when {
            ui.panel != Panel.NONE -> vm.setPanel(Panel.NONE)
            ui.srcPickerVisible -> vm.hideSrcPicker()    // 直播：先收信号源条（需求 足球#14；错误态开条时 BACK 也先收条）
            ui.fatalMsg != null -> nav.popBackStack()    // 错误兜底态：BACK 直接返回上一页
            // v1.21 乱跑修复：收控制条的同时挂「再按退出」提示并入双击窗口——
            // 原逻辑这击不算退出意图，进播放器（控制条短暂可见）后用户双 BACK
            // 只会收条+挂提示，第三击才退（实测要按 4~6 次，黑屏期方向键死锁）
            ui.controlsVisible -> {
                vm.hideControls()
                lastBackAt = android.os.SystemClock.uptimeMillis()
                backHint.value = "再按一次返回键退出播放"
            }
            else -> {
                // v1.19：交互计时改 uptimeMillis（墙钟跳变会误判双击窗口）
                val now = android.os.SystemClock.uptimeMillis()
                if (backHint.value != null || now - lastBackAt < 2500) nav.popBackStack()
                else { lastBackAt = now; backHint.value = "再按一次返回键退出播放" }
            }
        }
    }

    // 根键宿：直播模式收起信号源条后焦点回落（OK 才能再次呼出）
    // v1.13：navSink 注册隐藏节点——可持焦/可被引擎寻回，但不参与几何导航
    // v1.21 光标乱跑修复（2026-09-06 黑盒复现）：控制层销毁后 Compose 焦点恢复会把
    // 焦点甩到本全屏键宿上——它此前无 override，四向全 blocked（黑屏+方向键失灵
    // 死锁现场）。补上与 keySink 同语义的方向 override：任意方向键唤出控制条，
    // 焦点随即由 LaunchedEffect 落回中央播放钮，导航恢复正常。
    val rootFocus = remember { FocusRequester() }
    // 本页无标签栏：清空 topFocus，防 UP 到顶逃逸到已销毁屏的 tab 句柄
    LaunchedEffect(Unit) { com.qiubo.optimaltv.ui.components.OtvNav.topFocus = null }
    Box(
        Modifier
            .fillMaxSize()
            .background(Color.Black)
            .navSink(requester = rootFocus) { dir ->
                when {
                    ui.controlsVisible || ui.panel != Panel.NONE -> false
                    ui.isLive -> {
                        // 直播收起态：任意方向键呼信号源条（LiveSourcePicker 会把焦点
                        // 落到当前源 chip，恢复真实导航）
                        vm.toggleSrcPicker(); tick++; true
                    }
                    else -> { vm.showControls(); tick++; true }   // 影视：唤出控制条
                }
            }
            .onPreviewKeyEvent { e ->
                if (e.type != KeyEventType.KeyDown || e.nativeKeyEvent.repeatCount != 0) {
                    return@onPreviewKeyEvent false
                }
                val kc = e.nativeKeyEvent.keyCode
                when (kc) {
                    // 菜单键：影视=更多面板；直播=呼出/收起信号源选择条（需求 足球#14）。
                    // 错误态也放行：bb 解码失败的盒子可经此手动换源（bb 仍是自动默认）
                    KeyEvent.KEYCODE_MENU -> {
                        if (ui.isLive) {
                            vm.toggleSrcPicker()
                        } else {
                            vm.setPanel(Panel.MORE)
                        }
                        tick++; true
                    }
                    // 直播：OK 呼出信号源条（条已开时不拦截——交给聚焦 chip 完成切源）
                    KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER -> {
                        if (ui.isLive && ui.fatalMsg == null && !ui.srcPickerVisible) {
                            vm.toggleSrcPicker(); tick++; true
                        } else false
                    }
                    else -> false
                }
            },
    ) {
        // 视频渲染层：按画幅模式约束容器比例
        Box(
            Modifier
                .fillMaxSize()
                .then(aspectConstraint(ui.aspectOrdinal)),
            contentAlignment = Alignment.Center,
        ) {
            if (vm.hasEngine()) {
                // key=engineEpoch（引擎实例代数，2026-09-21 黑屏只剩声音修复）：
                // AndroidView factory 只跑一次，任何「换引擎实例」都必须换 key。旧版按
                // engineId 取 key，同类型重建（卡顿阶梯 RECREATE_ENGINE、直播 30s 自动
                // 重试、错误态手动重试）时 key 不变 → 视图树继续挂着已 release 的旧
                // PlayerView，新引擎的渲染面从未挂进窗口 ⇒ 整屏全黑、声音照常。
                // 口径与 TvScreen 视频层、移动版 PlayerScreen 统一。
                key(ui.engineEpoch) {
                    AndroidView(
                        factory = {
                            val v = vm.engine.renderView
                            // 同一渲染面若仍挂在旧容器上，先摘下来再交给新的 AndroidView
                            (v.parent as? android.view.ViewGroup)?.removeView(v)
                            v
                        },
                        modifier = Modifier.fillMaxSize(),
                    )
                }
            }
        }

        // 影院 scrim（控制条可见或未起播时淡入）
        val scrimAlpha by animateFloatAsState(
            targetValue = if ((ui.controlsVisible && !ui.isLive) || (ui.playState == EnginePlayState.IDLE && ui.fatalMsg == null)) 1f else 0f,
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

        // 错误兜底（防线三）
        ui.fatalMsg?.let { msg ->
            val errorFocus = remember { FocusRequester() }
            InitialFocusEffect(errorFocus, "error-retry")
            ErrorWidget(
                message = msg,
                onRetry = { vm.retry() },
                onBack = { nav.popBackStack() },
                focusRequester = errorFocus,
            )
        }

        val ps = rememberUiScale()
        val centerPlayFocus = remember { FocusRequester() }
        val scrubFocus = remember { FocusRequester() }
        val moreFocus = remember { FocusRequester() }
        if (ui.isLive) {
            // ---- 足球直播（需求 足球#14）：无任何菜单、无比分板；唯一控件=信号源选择条 ----
            LiveSourcePicker(ui, vm, Modifier.align(Alignment.BottomCenter), onTick = { tick++ }, rootFocus = rootFocus)
        } else {
            // ---- 影视：tvOS 播放器（需求 影视#3）----
            VodPlayerOverlay(
                ui = ui,
                vm = vm,
                onTick = { tick++ },
                onBack = ::onBackPressed,
                centerPlayFocus = centerPlayFocus,
                scrubFocus = scrubFocus,
                moreFocus = moreFocus,
            )
        }

        /* ⑭（2026-09-08）：TV 版投屏功能整体移除——v1.21 投屏入口胶囊与设备浮层删除
           （移动版保留；共享层 PlayerViewModel 的 DLNA 逻辑因双端同源不动，TV 侧无任何入口触达） */

        // 面板：更多（影片信息+选集[仅非电影]+换源+倍速+画幅）
        if (ui.panel == Panel.MORE) {
            MorePanel(
                ui = ui,
                vm = vm,
                onDismiss = { vm.setPanel(Panel.NONE) },
                // v1.19：VM 内直切换集（不走路由堆叠 VM/引擎实例，见 switchEpisode）
                onPickEpisode = { i ->
                    vm.switchEpisode(i)
                },
            )
        }

        // BACK 分层处理
        BackHandler { onBackPressed() }

        // 返回提示（需求 足球#5；需求 影视#3：统一贴屏幕最下方，组件内自带贴底边距）
        OtvHint(backHint.value)
    }
}

/** 播放器浮窗导航分组（需求 影视#1）：浮窗内控件归组，引擎只在本组内几何移动、绝不逃逸 */
private const val PANEL_NAV_GROUP = "player-panel"

/** 控制条三键组（更多/播放/进度条）独立分组：seek 语义之外的方向一律封锁 */
private const val CTL_NAV_GROUP = "player-ctl"

/**
 * 直播信号源选择条（需求 足球#14：直播播放器唯一控件）：
 * OK/菜单键呼出 → 焦点落当前源；左右移动、OK 切源（即时换线）；BACK 或 5s 无操作隐藏。
 * 默认源 = bb（原版足球直播2），由 bootLive 固定 index 0 起播。
 */
@Composable
private fun LiveSourcePicker(
    ui: PlayerUiState,
    vm: PlayerViewModel,
    modifier: Modifier,
    onTick: () -> Unit,
    rootFocus: FocusRequester,
) {
    val ps = rememberUiScale()
    // 呼出时焦点落「当前源」chip（重试可能重建线路表，按 lines 重建句柄）
    val chipFocus = remember(ui.lines) { List(ui.lines.size) { FocusRequester() } }
    LaunchedEffect(ui.srcPickerVisible, ui.activeLine, ui.lines) {
        if (!ui.srcPickerVisible) return@LaunchedEffect
        repeat(20) {
            val fr = chipFocus.getOrNull(ui.activeLine) ?: return@LaunchedEffect
            try { fr.requestFocus(); return@LaunchedEffect } catch (_: IllegalStateException) {
                kotlinx.coroutines.delay(50)
            }
        }
    }
    // 收起（BACK/超时/切面板）后焦点回根键宿，OK 可再次呼出
    LaunchedEffect(ui.srcPickerVisible) {
        if (!ui.srcPickerVisible) runCatching { rootFocus.requestFocus() }
    }
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
                var chipFocused by remember { mutableStateOf(false) }
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .then(if (active) Modifier.focusRequester(chipFocus[idx]) else Modifier)
                        .dpadFocusable(
                            scaleFocused = 1.05f,
                            focusedBg = Color.White,
                            // 播放器控件不画描边环（需求 影视#7 同款约定）
                            drawRing = false,
                            onFocusedChange = {
                                chipFocused = it
                                if (it) onTick()   // 浏览中不触发 5s 自动隐藏
                            },
                        ) { vm.switchLine(idx); onTick() }
                        .background(
                            if (active) Color(0x59FFFFFF) else Color(0x24FFFFFF),
                            RoundedCornerShape(12f.sx(ps)),
                        )
                        .padding(horizontal = 24f.sx(ps), vertical = 12f.sx(ps)),
                ) {
                    Text(
                        line.label,
                        color = if (active || chipFocused) OtvColors.Bg else Color.White,
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
 * tvOS 风格影视播放器浮层（参考截图 1:1）：
 * 标题+meta 左上 / 中央三键组（─10 / 播放暂停 / +10）/ 底部进度条（已播-轨道-剩余）/ 左下更多钮。
 * 焦点链：中央组 ↔ 进度条 ↔ 更多钮；菜单隐藏时根级 OK/上下唤出、左右 ±30s。
 */
@Composable
private fun VodPlayerOverlay(
    ui: PlayerUiState,
    vm: PlayerViewModel,
    onTick: () -> Unit,
    onBack: () -> Unit,
    centerPlayFocus: FocusRequester,
    scrubFocus: FocusRequester,
    moreFocus: FocusRequester,
) {
    val keySink = remember { FocusRequester() }
    InitialFocusEffect(keySink, "player-sink")

    // 控制条出现：焦点落中央播放钮；隐藏：焦点回键宿
    LaunchedEffect(ui.controlsVisible, ui.panel, ui.fatalMsg) {
        if (ui.fatalMsg != null) return@LaunchedEffect
        if (ui.controlsVisible && ui.panel == Panel.NONE) {
            repeat(20) {
                try { centerPlayFocus.requestFocus(); return@LaunchedEffect } catch (_: IllegalStateException) {
                    kotlinx.coroutines.delay(50)
                }
            }
        } else if (!ui.controlsVisible && ui.panel == Panel.NONE) {
            runCatching { keySink.requestFocus() }
        }
    }

    /** 控制条隐藏态的方向键处理（引擎 override 通道；OK/数字键仍走 Compose 事件） */
    fun consumeDirKey(dir: Int): Boolean {
        when (dir) {
            com.qiubo.optimaltv.ui.components.NAV_LEFT, com.qiubo.optimaltv.ui.components.NAV_RIGHT -> {
                // 需求 影视#8：菜单隐藏时左右快进自动弹出菜单栏
                if (!ui.controlsVisible) vm.showControls()
                vm.seekBy(if (dir == com.qiubo.optimaltv.ui.components.NAV_LEFT) -PlaybackPolicy.SEEK_STEP_MS else PlaybackPolicy.SEEK_STEP_MS)
                onTick(); return true
            }
            com.qiubo.optimaltv.ui.components.NAV_UP, com.qiubo.optimaltv.ui.components.NAV_DOWN -> {
                vm.toggleControls(); return true
            }
        }
        return false
    }

    /** 控制条隐藏态的根级按键处理（Compose 事件通道：OK/数字） */
    fun consumeKey(keyCode: Int): Boolean {
        when (keyCode) {
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER -> { vm.toggleControls(); return true }
            in KeyEvent.KEYCODE_0..KeyEvent.KEYCODE_9 -> { vm.inputDigit(keyCode - KeyEvent.KEYCODE_0); return true }
        }
        return false
    }

    Box(Modifier.fillMaxSize()) {
        // 隐形键宿：菜单隐藏时持有焦点，保证 OK/左右/数字响应
        Box(
            Modifier
                .size(1.dp)
                .navSink(
                    requester = keySink,
                    override = { dir ->
                        if (ui.controlsVisible || ui.panel != Panel.NONE) false else consumeDirKey(dir)
                    },
                )
                .onPreviewKeyEvent { e ->
                    if (e.type != KeyEventType.KeyDown) return@onPreviewKeyEvent false
                    if (ui.controlsVisible || ui.panel != Panel.NONE) return@onPreviewKeyEvent false
                    consumeKey(e.nativeKeyEvent.keyCode)
                },
        )

        val ps = rememberUiScale()
        val posMs = ui.positionMs
        val durMs = ui.durationMs
        if (ui.controlsVisible && ui.fatalMsg == null) {
            // 「更多」钮（需求 影视#5）：移到右上角，控制条任意控件按 UP 可达；
            // DOWN 回进度行播放钮，UP/LEFT 隐藏控制条
            Box(
                Modifier
                    .align(Alignment.TopEnd)
                    .padding(end = 80f.sx(ps), top = 90f.sx(ps)),
            ) {
                CircleButtonBase(
                    size = 52.dp,
                    focusRequester = moreFocus,
                    navGroup = CTL_NAV_GROUP,
                    navOverride = { dir ->
                        when (dir) {
                            com.qiubo.optimaltv.ui.components.NAV_DOWN -> { runCatching { centerPlayFocus.requestFocus() }; onTick(); true }
                            com.qiubo.optimaltv.ui.components.NAV_UP, com.qiubo.optimaltv.ui.components.NAV_LEFT -> { vm.hideControls(); onTick(); true }
                            else -> false
                        }
                    },
                    onClick = { vm.setPanel(Panel.MORE) },
                ) { c ->
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

            // 底部：标题+meta（需求 影视#4：从左上角移到控制条上方）→ 进度行（播放暂停钮 - 已播 - 轨道 - 剩余）
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
                var scrubFocused by remember { mutableStateOf(false) }
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    // 播放/暂停（进度条前面）：唤起菜单自动聚焦此钮（需求 影视#6）
                    // 需求 影视#3：按钮缩小（64→30dp），与右侧进度条视觉高度相当
                    Box {
                        CircleButtonBase(
                            size = 30.dp,
                            focusRequester = centerPlayFocus,
                            navGroup = CTL_NAV_GROUP,
                            navOverride = { dir ->
                                when (dir) {
                                    com.qiubo.optimaltv.ui.components.NAV_LEFT -> { vm.seekBy(-PlaybackPolicy.SEEK_STEP_MS); onTick(); true }
                                    com.qiubo.optimaltv.ui.components.NAV_RIGHT -> { vm.seekBy(PlaybackPolicy.SEEK_STEP_MS); onTick(); true }
                                    com.qiubo.optimaltv.ui.components.NAV_UP -> { runCatching { moreFocus.requestFocus() }; onTick(); true }
                                    else -> false
                                }
                            },
                            onClick = { vm.togglePlay() },
                        ) { on ->
                            PlayPauseIcon(playing = ui.isPlaying, iconColor = on, iconSize = 13.dp)
                        }
                    }
                    Spacer(Modifier.width(24f.sx(ps)))
                    Text(formatTime(posMs), color = Color(0xE6FFFFFF), fontSize = 24f.sxs(ps), fontWeight = FontWeight.SemiBold)
                    Box(
                        Modifier
                            .weight(1f)
                            .padding(horizontal = 28.dp),
                    ) {
                        Box(
                            Modifier
                                .focusRequester(scrubFocus)
                                .dpadFocusable(
                                    scaleFocused = 1f,
                                    focusedBg = Color.Transparent,
                                    // 需求 影视#7：进度条聚焦不画描边环，聚焦态用加粗轨道+放大圆点表达
                                    drawRing = false,
                                    onFocusedChange = { scrubFocused = it },
                                    navGroup = CTL_NAV_GROUP,
                                    navOverride = { dir ->
                                        when (dir) {
                                            com.qiubo.optimaltv.ui.components.NAV_LEFT -> { vm.seekBy(-PlaybackPolicy.SEEK_STEP_MS); onTick(); true }
                                            com.qiubo.optimaltv.ui.components.NAV_RIGHT -> { vm.seekBy(PlaybackPolicy.SEEK_STEP_MS); onTick(); true }
                                            com.qiubo.optimaltv.ui.components.NAV_UP -> { runCatching { moreFocus.requestFocus() }; onTick(); true }
                                            else -> false
                                        }
                                    },
                                ) { vm.togglePlay() }
                                .padding(vertical = 10.dp),
                        ) {
                            Box(
                                Modifier
                                    .fillMaxWidth()
                                    .height(if (scrubFocused) 10.dp else 6.dp)
                                    .background(
                                        if (scrubFocused) Color(0x59FFFFFF) else Color(0x4DFFFFFF),
                                        RoundedCornerShape(5.dp),
                                    ),
                            ) {
                                val frac = if (durMs > 0) posMs.toFloat() / durMs else 0f
                                Box(
                                    Modifier
                                        .fillMaxHeight()
                                        .fillMaxWidth(frac.coerceIn(0.005f, 1f)),
                                ) {
                                    Box(Modifier.fillMaxSize().background(Color.White, RoundedCornerShape(5.dp)))
                                    Box(
                                        Modifier
                                            .align(Alignment.CenterEnd)
                                            .size(if (scrubFocused) 22.dp else 14.dp)
                                            .background(Color.White, CircleShape),
                                    )
                                }
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
                CircleButtonBase(size = 96.dp, onClick = { vm.togglePlay() }) { on ->
                    PlayPauseIcon(playing = true, iconColor = on, iconSize = 40.dp)
                }
            }
        }
    }
}

/**
 * 右侧悬浮栏「更多」：影片信息 + 选集（仅电视剧/动漫/短剧）+ 换源 + 倍速 + 画幅（需求 影视#3）
 * 需求⑨修订（2026-08-31）：**电影不显示选集区**（含 HD中字/HD国语 这类多版本电影），
 * 电视剧/动漫/短剧保留选集（分页 + 网格，选集跳转继续播）。
 * 浮窗落焦兜底链：当前集格（选集区显示时）→ 换源首 chip → 倍速首 chip。
 * 需求 影视#1（2026-08-31）：浮窗光标无法移动修复——
 * ① 浮窗内没有行带导航修饰符，默认 Compose 几何搜索会把焦点送出浮窗（更多钮/
 *    进度条都在浮窗背后）→ 面板级 onPreviewKeyEvent 接管四方向，只在本浮窗
 *    navGroup="panel" 的节点内几何移动，无候选即封锁，焦点绝不逃逸；
 * ② 换源线路 14+ 条超宽被面板裁切 → FlowRow 换行铺满面板宽（需求⑳ 全部源一次可见）。
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun MorePanel(
    ui: PlayerUiState,
    vm: PlayerViewModel,
    onDismiss: () -> Unit,
    onPickEpisode: (Int) -> Unit,
) {
    val panelFocus = remember { FocusRequester() }
    InitialFocusEffect(panelFocus, "panel-more")
    Box(Modifier.fillMaxSize()) {
        // 左侧暗区：视觉压暗视频（不可聚焦；BACK 关闭浮窗）
        Box(Modifier.fillMaxSize().background(Color(0x66000000)))
        // v1.13：面板内四方向由 OtvNav 引擎按 navGroup=PANEL_NAV_GROUP 组内几何
        // 移动（节点自带分组声明），无候选即封锁——焦点绝不逃逸到浮窗背后的控件，
        // 面板容器不再需要任何按键拦截修饰符
        // 需求⑰（2026-09-11）：面板挂靠 ScrollState 导航容器——非 lazy 滚动容器里
        // 视口外节点 bounds 上报 Rect.Zero（实测池转储全零），不挂靠则几何搜索永远
        // 找不到屏幕下方区块，DOWN 封锁在选集网格末行（用户报障「光标无法移到最下面」）。
        // 挂靠后引擎在几何封锁时步进滚动面板并重试，落焦 ensureVisible 补齐边距。
        val panelScroll = rememberScrollState()
        val panelNav = remember(panelScroll) { scrollNavContainer(panelScroll) }
        ProvideNavScrolls(vertical = panelNav) {
        Column(
            Modifier
                .align(Alignment.CenterEnd)
                .width(560.dp)
                .fillMaxHeight()
            .background(
                Color(0xE9121216),
                RoundedCornerShape(topStart = 24.dp, bottomStart = 24.dp),
            )
            .verticalScroll(panelScroll)
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
            // 选集（需求⑨修订：仅电视剧/动漫/短剧显示，电影不显示——
            // 含 HD中字/HD国语 这类多版本电影）。≥20 集按 10 集/页 + 分页 chip；<20 集全量展示
            if (!ui.isMovie && ui.epCount > 1) {
                SectionLabel("选集")
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
                                .dpadFocusable(navGroup = PANEL_NAV_GROUP) { page = p }
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
                // 2026-09-19 集数不全修复：展示范围收口到 EpisodeMenuPolicy.visibleRange
                //（<20 集全量；旧版 11~19 集落在无 chip 的第 2 页，前 10 集够不着——
                // 老板报障《深渊无间》16 集只见 11-16）
                val pageEps = EpisodeMenuPolicy.visibleRange(ui.epCount, page)
                val focusTarget = pageEps.indexOf(ui.epIndex).takeIf { it >= 0 } ?: 0
                LazyVerticalGrid(
                    columns = GridCells.Fixed(4),
                    modifier = Modifier.height(((pageEps.count() + 3) / 4 * 52).dp),
                ) {
                    items(pageEps.toList()) { i ->
                        Box(
                            contentAlignment = Alignment.Center,
                            modifier = Modifier
                                .padding(4.dp)
                                .then(if (i - pageEps.first == focusTarget) Modifier.focusRequester(panelFocus) else Modifier)
                                .dpadFocusable(navGroup = PANEL_NAV_GROUP) { onPickEpisode(i) }
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
            // 换源（需求⑳ 2026-09-11：FlowRow 换行铺满面板宽——旧单行 horizontalScroll
            // 只能看到前几个源，其余被裁切不可见；全部线路一次展示，跨行导航由面板
            // 滚动挂靠（需求⑰）支持）
            // ⑭（2026-09-08）：「投屏」小节整体移除（TV 版投屏功能全删，移动版保留）
            if (ui.lines.size > 1) {
            SectionLabel("换源线路")
                FlowRow(
                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp),
                ) {
                    ui.lines.forEachIndexed { idx, line ->
                        Box(
                            modifier = Modifier
                                // 需求⑨修订：选集区未显示（电影/单集）时面板落焦落换源首 chip
                                .then(
                                    if (idx == 0 && (ui.isMovie || ui.epCount <= 1)) {
                                        Modifier.focusRequester(panelFocus)
                                    } else Modifier
                                )
                                .dpadFocusable(navGroup = PANEL_NAV_GROUP) { vm.switchLine(idx) }
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
                PlayerViewModel.SPEED_STEPS.forEachIndexed { idx, sp ->
                    val active = kotlin.math.abs(sp - ui.speed) < 0.01f
                    Box(
                        contentAlignment = Alignment.Center,
                        modifier = Modifier
                            .then(
                                if ((ui.isMovie || ui.epCount <= 1) && ui.lines.size <= 1 && idx == 0) {
                                    Modifier.focusRequester(panelFocus)
                                } else Modifier
                            )
                            .dpadFocusable(navGroup = PANEL_NAV_GROUP) { vm.setSpeed(sp) }
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
                            .dpadFocusable(navGroup = PANEL_NAV_GROUP) { vm.setAspect(mode.ordinal) }
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
            Spacer(Modifier.height(28.dp))
        }
        } // ProvideNavScrolls(panelNav)
    }
}

@Composable
private fun SectionLabel(text: String) {
    Text(
        text, color = TextPrimary, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold,
        modifier = Modifier.padding(bottom = 10.dp),
    )
}

/** 毛玻璃圆形按钮基座（聚焦纯白底 + 图标反黑 + 放大 1.1） */
@Composable
private fun CircleButtonBase(
    size: Dp = 56.dp,
    focusRequester: FocusRequester? = null,
    navGroup: String? = null,
    navOverride: ((Int) -> Boolean)? = null,
    onClick: () -> Unit,
    content: @Composable (iconColor: Color) -> Unit,
) {
    var btnFocused by remember { mutableStateOf(false) }
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .then(if (focusRequester != null) Modifier.focusRequester(focusRequester) else Modifier)
            .size(size)
            // 深色毛玻璃底先绘制，dpadFocusable 再在其上绘制聚焦白底，避免高亮被覆盖
            .background(Color(0x66141414), CircleShape)
            .dpadFocusable(
                scaleFocused = 1.1f,
                focusedBg = Color.White,
                focusedBgRadius = size / 2,
                // 需求 影视#7：播放器控件不画描边环，聚焦高亮只用纯白底 + 图标反黑
                drawRing = false,
                onFocusedChange = { btnFocused = it },
                navGroup = navGroup,
                navOverride = navOverride,
                onClick = onClick,
            )
            .border(1.dp, Color(0x1FFFFFFF), CircleShape),
    ) {
        content(if (btnFocused) Color(0xFF111111) else Color.White)
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
    focusRequester: FocusRequester? = null,
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
                        .then(if (focusRequester != null) Modifier.focusRequester(focusRequester) else Modifier)
                        .dpadFocusable(onClick = onRetry)
                        .background(AccentBlue, RoundedCornerShape(12.dp))
                        .padding(horizontal = 34.dp, vertical = 12.dp),
                ) { Text("重试", color = Color.White, style = MaterialTheme.typography.labelLarge) }
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .dpadFocusable(onClick = onBack)
                        .background(Color(0x2EFFFFFF), RoundedCornerShape(12.dp))
                        .padding(horizontal = 34.dp, vertical = 12.dp),
                ) { Text("返回", color = Color.White, style = MaterialTheme.typography.labelLarge) }
            }
        }
    }
}

/** 画幅模式 → 渲染容器约束（原始/裁切交给 PlayerView 内部 resize） */
private fun aspectConstraint(ord: Int): Modifier =
    when (val m = com.qiubo.optimaltv.data.prefs.AspectMode.of(ord)) {
        com.qiubo.optimaltv.data.prefs.AspectMode.R16_9 -> Modifier.fillMaxSize().aspectRatio(16f / 9f)
        com.qiubo.optimaltv.data.prefs.AspectMode.R4_3 -> Modifier.fillMaxSize().aspectRatio(4f / 3f)
        else -> Modifier.fillMaxSize().then(m.ratio?.let { Modifier.aspectRatio(it) } ?: Modifier)
    }

/* ⑭（2026-09-08 需求⑭）：TV 版投屏功能全部移除——CastDevicePanel/CastGlyph 随入口一并删除
   （移动版同款组件在 mobile 工程自有 PlayerScreen，不受影响）。 */

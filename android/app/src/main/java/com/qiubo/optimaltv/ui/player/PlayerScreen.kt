package com.qiubo.optimaltv.ui.player

import android.app.Activity
import android.view.KeyEvent
import android.view.WindowManager
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
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
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.relocation.BringIntoViewRequester
import androidx.compose.foundation.relocation.bringIntoViewRequester
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
import androidx.compose.runtime.rememberCoroutineScope
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
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFontFamilyResolver
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.TextMeasurer
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Constraints
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
import com.qiubo.optimaltv.playback.EnginePlayState
import com.qiubo.optimaltv.ui.components.AppleLoading
import com.qiubo.optimaltv.ui.components.InitialFocusEffect
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.ProvideNavScrolls
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.components.navSink
import com.qiubo.optimaltv.ui.components.scrollNavContainer
import com.qiubo.optimaltv.ui.components.NAV_DOWN
import com.qiubo.optimaltv.ui.components.NAV_LEFT
import com.qiubo.optimaltv.ui.components.NAV_RIGHT
import com.qiubo.optimaltv.ui.components.NAV_UP
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sxs
import com.qiubo.optimaltv.ui.theme.sx
import java.net.URLDecoder
import kotlinx.coroutines.launch

/**
 * 球迹直播播放页只提供比赛画面和信号源选择。
 * 左右键切换信号源，返回键退出；
 * - 球迹直播（需求 足球#14）：无任何菜单、无比分板；唯一控件=信号源选择条
 *   （OK/菜单键呼出，左右移动、OK 切换，BACK/5s 自动隐藏）；默认源=上次可用线路或服务端首条；
 *   两次返回退出（首次提示）
 * - KEEP_SCREEN_ON / 断点续播 / 看门狗换线（VM 承担）
 */
@Composable
fun PlayerScreen(nav: NavController, matchIdArg: String) {
    val matchId = URLDecoder.decode(matchIdArg, "UTF-8")
    val vm: PlayerViewModel = viewModel(
        key = "football-player:$matchId",
        factory = playerViewModelFactory(matchId),
    )
    val ui by vm.ui.collectAsStateWithLifecycle()
    val activity = LocalContext.current as Activity
    DisposableEffect(Unit) {
        activity.window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        onDispose { activity.window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON) }
    }
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_PAUSE -> vm.onHostPause()
                Lifecycle.Event.ON_RESUME -> vm.onHostResume()
                else -> Unit
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }
    var tick by remember { mutableIntStateOf(0) }
    LaunchedEffect(ui.srcPickerVisible, ui.fatalMsg, tick) {
        if (ui.srcPickerVisible && ui.fatalMsg == null) {
            kotlinx.coroutines.delay(5_000)
            vm.hideSrcPicker()
        }
    }
    val backHint = remember { mutableStateOf<String?>(null) }
    var lastBackAt by remember { mutableLongStateOf(0L) }
    LaunchedEffect(backHint.value) {
        if (backHint.value != null) {
            kotlinx.coroutines.delay(2_500)
            backHint.value = null
        }
    }
    fun onBackPressed() {
        when {
            ui.srcPickerVisible -> vm.hideSrcPicker()
            ui.fatalMsg != null -> nav.popBackStack()
            else -> {
                val now = android.os.SystemClock.uptimeMillis()
                if (backHint.value != null || now - lastBackAt < 2_500) nav.popBackStack()
                else {
                    lastBackAt = now
                    backHint.value = "再按一次返回键退出播放"
                }
            }
        }
    }
    val rootFocus = remember { FocusRequester() }
    // 播放页没有顶部标签或菜单，进入页面后立即把焦点交给画面根宿，
    // 让第一次上下左右/OK 就能被遥控器处理，不依赖用户先按第二次键触发自愈。
    InitialFocusEffect(rootFocus, "player-root", enabled = !ui.srcPickerVisible)
    LaunchedEffect(Unit) { com.qiubo.optimaltv.ui.components.OtvNav.topFocus = rootFocus }
    LaunchedEffect(ui.fatalMsg, ui.srcPickerVisible) {
        if (ui.fatalMsg != null && !ui.srcPickerVisible) {
            runCatching { rootFocus.requestFocus() }
        }
    }
    Box(
        Modifier
            .fillMaxSize()
            .background(Color.Black)
            .navSink(requester = rootFocus) {
                if (!ui.srcPickerVisible) vm.toggleSrcPicker()
                tick++
                true
            }
            .onPreviewKeyEvent { event ->
                if (event.type != KeyEventType.KeyDown || event.nativeKeyEvent.repeatCount != 0) return@onPreviewKeyEvent false
                when (event.nativeKeyEvent.keyCode) {
                    KeyEvent.KEYCODE_BACK -> { onBackPressed(); true }
                    KeyEvent.KEYCODE_MENU -> { vm.toggleSrcPicker(); tick++; true }
                    KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER -> {
                        if (!ui.srcPickerVisible) { vm.toggleSrcPicker(); tick++; true } else false
                    }
                    else -> false
                }
            },
    ) {
        if (vm.hasEngine()) {
            key(ui.engineEpoch) {
                AndroidView(
                    factory = {
                        val renderView = vm.engine.renderView
                        (renderView.parent as? android.view.ViewGroup)?.removeView(renderView)
                        renderView
                    },
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
        if (ui.sourceResolving && ui.lines.isEmpty()) {
            Text("正在检测可用线路…", color = OtvColors.TextSecondary, modifier = Modifier.align(Alignment.Center))
        }
        if (ui.playState == EnginePlayState.BUFFERING && ui.fatalMsg == null) {
            AppleLoading(modifier = Modifier.align(Alignment.Center), size = 56.dp)
        }
        ui.fatalMsg?.let { LivePlaybackFailureHint(it, Modifier.align(Alignment.TopCenter)) }
        LiveSourcePicker(ui, vm, Modifier.align(Alignment.BottomCenter), onTick = { tick++ }, rootFocus = rootFocus)
        BackHandler { onBackPressed() }
        OtvHint(backHint.value)
    }
}

private fun playerViewModelFactory(matchId: String) =
    object : androidx.lifecycle.ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : androidx.lifecycle.ViewModel> create(modelClass: Class<T>): T =
            PlayerViewModel(matchId, Graph.settings) as T
    }
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
    val retryFocus = remember(ui.lines) { FocusRequester() }
    LaunchedEffect(ui.srcPickerVisible, ui.activeLine, ui.lines, ui.sourceResolving) {
        if (!ui.srcPickerVisible) return@LaunchedEffect
        if (ui.lines.isEmpty()) {
            if (ui.sourceResolving) {
                runCatching { rootFocus.requestFocus() }
                return@LaunchedEffect
            }
            repeat(20) {
                try { retryFocus.requestFocus(); return@LaunchedEffect } catch (_: IllegalStateException) {
                    kotlinx.coroutines.delay(50)
                }
            }
            return@LaunchedEffect
        }
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
            .background(Color(0xF2151815), RoundedCornerShape(20f.sx(ps)))
            .padding(horizontal = 26f.sx(ps), vertical = 20f.sx(ps)),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            "信号源",
            color = OtvColors.TextSecondary, fontSize = 20f.sxs(ps),
            modifier = Modifier.padding(bottom = 12f.sx(ps)),
        )
        if (ui.lines.isEmpty()) {
            if (ui.sourceResolving) {
                Text("正在检测可用线路…", color = OtvColors.TextPrimary, fontSize = 20f.sxs(ps))
            } else {
                var retryFocused by remember { mutableStateOf(false) }
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .focusRequester(retryFocus)
                        .dpadFocusable(
                            scaleFocused = 1.05f,
                            // 播放器控件不画描边环，保证比赛画面干净；聚焦 = 白底深字
                            drawRing = false,
                            onFocusedChange = { retryFocused = it },
                        ) {
                            vm.retry(); onTick()
                        }
                        .background(if (retryFocused) Color.White else OtvColors.Brand, RoundedCornerShape(50f.sx(ps)))
                        .padding(horizontal = 24f.sx(ps), vertical = 12f.sx(ps)),
                ) {
                    Text(
                        "重新检测线路",
                        color = if (retryFocused) OtvColors.Bg else OtvColors.Bg,
                        fontSize = 15f.sxs(ps), fontWeight = FontWeight.Medium,
                    )
                }
            }
        } else {
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
                                // 规范选源条：选中 = brand 底白字，未选 = surface 底次级字；
                                // 播放器控件不画描边环，聚焦 = 白底深字（画面干净且光标醒目）
                                drawRing = false,
                                onFocusedChange = {
                                    chipFocused = it
                                    if (it) onTick()   // 浏览中不触发 5s 自动隐藏
                                },
                            ) { vm.switchLine(idx); onTick() }
                            .background(
                                when {
                                    chipFocused -> Color.White
                                    active -> OtvColors.Brand
                                    else -> OtvColors.Surface
                                },
                                RoundedCornerShape(50f.sx(ps)),
                            )
                            .padding(horizontal = 24f.sx(ps), vertical = 12f.sx(ps)),
                    ) {
                        Text(
                            line.label,
                            color = when {
                                chipFocused -> OtvColors.Bg
                                active -> OtvColors.Bg
                                else -> OtvColors.TextSecondary
                            },
                            fontSize = 15f.sxs(ps),
                            fontWeight = if (active) FontWeight.SemiBold else FontWeight.Medium,
                            maxLines = 1,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun LivePlaybackFailureHint(message: String, modifier: Modifier = Modifier) {
    Column(
        modifier
            .padding(top = 24.dp)
            .background(Color(0xF2151815), RoundedCornerShape(12.dp))
            .padding(horizontal = 18.dp, vertical = 12.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(3.dp),
    ) {
        Text("当前线路无法播放", color = OtvColors.TextPrimary, style = MaterialTheme.typography.titleSmall)
        Text(
            message,
            color = OtvColors.TextSecondary,
            style = MaterialTheme.typography.bodySmall,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
        Text(
            "按 OK 或方向键打开线路列表重试",
            color = OtvColors.TextSecondary,
            style = MaterialTheme.typography.bodySmall,
        )
    }
}

package com.qiubo.optimaltv.ui.player

import android.app.Activity
import android.view.WindowManager
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectVerticalDragGestures
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
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.horizontalScroll
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.Shadow
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.translate
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.platform.testTag
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
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.playback.EnginePlayState
import com.qiubo.optimaltv.ui.components.AppleLoading
import com.qiubo.optimaltv.ui.components.MobileUiTags
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.tapCard
import com.qiubo.optimaltv.ui.theme.Bg
import com.qiubo.optimaltv.ui.theme.Brand
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import com.qiubo.optimaltv.ui.theme.SurfaceDark
import com.qiubo.optimaltv.ui.theme.TextPrimary
import com.qiubo.optimaltv.ui.theme.TextSecondary
import java.net.URLDecoder
import kotlin.math.roundToInt

/**
 * 移动端球迹直播播放页只提供比赛画面和信号源选择。
 * 左右滑动切换信号源，返回键退出；
 * - 球迹直播（需求 足球#14）：无任何菜单、无比分板；唯一控件=信号源选择条
 *   （单击画面呼出/收起，点 chip 即时换线，BACK/5s 自动隐藏）；两次返回退出
 * - 播放会话与 TV 版共用同一 PlayerViewModel（断点续播/看门狗换线/90s 续签/三段回退全保留）
 */
@Composable
fun PlayerScreen(nav: NavController, matchIdArg: String) {
    val matchId = URLDecoder.decode(matchIdArg, "UTF-8")
    val vm: PlayerViewModel = viewModel(
        key = "football-player:$matchId",
        factory = mobilePlayerViewModelFactory(matchId),
    )
    val ui by vm.ui.collectAsStateWithLifecycle()
    val activity = LocalContext.current as Activity
    DisposableEffect(Unit) {
        activity.window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        onDispose {
            activity.window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            activity.requestedOrientation = android.content.pm.ActivityInfo.SCREEN_ORIENTATION_FULL_USER
        }
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
    fun onBackPressed() {
        if (ui.srcPickerVisible && ui.fatalMsg == null) vm.hideSrcPicker() else nav.popBackStack()
    }
    Box(
        Modifier
            .fillMaxSize()
            .background(Color.Black)
            .pointerInput(Unit) {
                detectTapGestures(onTap = { vm.toggleSrcPicker(); tick++ })
            },
    ) {
        if (vm.hasEngine()) {
            key(ui.engineEpoch) {
                AndroidView(
                    factory = {
                        val renderView = vm.engine.renderView
                        (renderView.parent as? android.view.ViewGroup)?.removeView(renderView)
                        (renderView as? androidx.media3.ui.PlayerView)?.apply {
                            useController = false
                            isClickable = false
                            isLongClickable = false
                        }
                        renderView
                    },
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
        if (ui.sourceResolving && ui.lines.isEmpty()) {
            Text("正在检测可用线路…", color = Color.White.copy(alpha = 0.7f), modifier = Modifier.align(Alignment.Center))
        }
        if (ui.playState == EnginePlayState.BUFFERING && ui.fatalMsg == null) {
            AppleLoading(modifier = Modifier.align(Alignment.Center), size = 56.dp)
        }
        LiveSourcePicker(
            ui = ui,
            onSelectLine = vm::switchLine,
            onRetry = vm::retry,
            modifier = Modifier.align(Alignment.BottomCenter),
            onTick = { tick++ },
        )
        BackHandler { onBackPressed() }
        OtvHint(null)
    }
}

private fun mobilePlayerViewModelFactory(matchId: String) =
    object : androidx.lifecycle.ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : androidx.lifecycle.ViewModel> create(modelClass: Class<T>): T =
            PlayerViewModel(matchId, Graph.settings) as T
    }
@Composable
internal fun LiveSourcePicker(
    ui: PlayerUiState,
    onSelectLine: (Int) -> Unit,
    onRetry: () -> Unit,
    modifier: Modifier,
    onTick: () -> Unit,
) {
    val ps = rememberUiScale()
    // 竖屏直播不渲染常驻操作区（v2），全屏入口挪进信号源条右端
    val portrait = androidx.compose.ui.platform.LocalConfiguration.current.orientation ==
        android.content.res.Configuration.ORIENTATION_PORTRAIT
    val activity = LocalContext.current as? Activity
    // A live failure is shown inline with the source controls. Keep the route picker open
    // while failed so switching sources never requires dismissing a blocking error dialog.
    if (!ui.srcPickerVisible && ui.fatalMsg == null) return

    Column(
        modifier
            .then(if (ui.fatalMsg != null) Modifier.fillMaxWidth(0.94f) else Modifier)
            .padding(bottom = 56f.sx(ps))
            .background(Color(0xE6151815), RoundedCornerShape(20f.sx(ps)))
            .padding(horizontal = 26f.sx(ps), vertical = 20f.sx(ps)),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        ui.fatalMsg?.let { message ->
            Row(
                modifier = Modifier.fillMaxWidth().padding(bottom = 14f.sx(ps)),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(16f.sx(ps)),
            ) {
                Column(Modifier.weight(1f), horizontalAlignment = Alignment.Start) {
                    Text(
                        "无法播放",
                        color = TextPrimary,
                        fontSize = 19f.sxs(ps),
                        fontWeight = FontWeight.SemiBold,
                        modifier = Modifier.testTag("live-playback-error-title"),
                    )
                    Text(
                        message,
                        color = TextSecondary,
                        fontSize = 15f.sxs(ps),
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.testTag("live-playback-error-message"),
                    )
                }
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .clip(RoundedCornerShape(10f.sx(ps)))
                        .tapCard { onRetry(); onTick() }
                        .background(Brand, RoundedCornerShape(10f.sx(ps)))
                        .padding(horizontal = 16f.sx(ps), vertical = 10f.sx(ps))
                        .testTag("live-playback-retry"),
                ) {
                    Text("重新检测", color = Color.Black, fontSize = 16f.sxs(ps))
                }
            }
        }
        Text(
            "信号源",
            color = TextSecondary, fontSize = 20f.sxs(ps),
            modifier = Modifier.padding(bottom = 12f.sx(ps)),
        )
        if (ui.sourceResolving && ui.lines.isEmpty()) {
            Text("正在检测可用线路…", color = TextSecondary, fontSize = 20f.sxs(ps))
        }
        // 线路数量会随后端变化，窄屏/竖屏时允许横向滑动，避免全屏钮或末条线路
        // 被挤出可点击区域，保证触屏仍能选到每个可用源。
        Row(
            modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(14f.sx(ps)),
        ) {
            ui.lines.forEachIndexed { idx, line ->
                val active = idx == ui.activeLine
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .clip(RoundedCornerShape(50))
                        .tapCard { onSelectLine(idx); onTick() }
                        .background(
                            // SPEC v1「选源条」：选中 = brand 底白字；未选 = surface 底 textSecondary
                            if (active) Brand else SurfaceDark,
                            RoundedCornerShape(50),
                        )
                        .padding(horizontal = 24f.sx(ps), vertical = 12f.sx(ps))
                        .testTag("live-source-$idx"),
                ) {
                    Text(
                        line.label,
                        color = if (active) Color.Black else TextSecondary,
                        fontSize = 25f.sxs(ps),
                        fontWeight = if (active) FontWeight.SemiBold else FontWeight.Normal,
                        maxLines = 1,
                    )
                }
            }
            // 竖屏全屏小钮（替代原常驻操作区的「全屏播放」文字钮）
            if (portrait) {
                CircleButton(size = 30.dp, onClick = {
                    activity?.requestedOrientation =
                        android.content.pm.ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                    onTick()
                }) { c -> FullscreenGlyph(color = c, size = 14.dp) }
            }
        }
    }
}

@Composable
private fun FullscreenGlyph(color: Color, size: Dp) {
    Canvas(Modifier.size(size)) {
        val w = this.size.width
        val h = this.size.height
        val s = this.size.minDimension
        val stroke = s * 0.11f
        val arm = s * 0.30f
        val margin = s * 0.08f
        fun corner(cx: Float, cy: Float, dx: Float, dy: Float) {
            drawLine(color, Offset(cx + dx * arm, cy), Offset(cx, cy), strokeWidth = stroke)
            drawLine(color, Offset(cx, cy + dy * arm), Offset(cx, cy), strokeWidth = stroke)
        }
        corner(margin, margin, 1f, 1f)
        corner(w - margin, margin, -1f, 1f)
        corner(margin, h - margin, 1f, -1f)
        corner(w - margin, h - margin, -1f, -1f)
    }
}

@Composable
private fun CircleButton(
    size: Dp,
    onClick: () -> Unit,
    content: @Composable (Color) -> Unit,
) {
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .size(size)
            .clip(CircleShape)
            .tapCard(onClick)
            .background(SurfaceDark, CircleShape)
            .border(1.dp, com.qiubo.optimaltv.ui.theme.Divider, CircleShape),
    ) {
        content(Color.White)
    }
}

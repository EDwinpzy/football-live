package com.qiubo.optimaltv.ui.components

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.lazy.LazyListItemInfo
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.grid.LazyGridItemInfo
import androidx.compose.foundation.lazy.grid.LazyGridState
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.draw.scale
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusEvent
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.layout.positionInWindow
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * ===========================================================================
 * OtvNav 集中式遥控导航引擎（v1.13 彻底重构，2026-09-01）
 * ===========================================================================
 * 设计参考：BBC lrud-spatial / W3C Spatial Navigation / Mozilla SpatialNavigator
 * 的「方向候选打分」模型，结合本应用全网格/行带布局定制。
 *
 * 旧架构病灶（v1.4~v1.12 补丁堆叠）：
 *  1. 按键处理散落在 focusRow(预览) / crossRowNav(冒泡) / scrollRowNav(预览) /
 *     各屏 onPreviewKeyEvent / MainActivity FocusBus 五层——同一按键可能被
 *     多层重复处理，也可能层间漏接（「有些地方去不了」）；
 *  2. 未被消费的方向键漏给 Compose 默认几何搜索，其兜底会斜跳任意节点（「乱跑」）；
 *  3. 目标行未组合时靠 animateScrollBy+delay 轮询重试（80~90ms×4），手感发闷
 *     （「不流畅」）；长按 repeat 一律丢弃，按住方向键没有连续移动。
 *
 * 新架构：**单一分发 + 确定性几何 + 精确滚动找回**。
 *  - MainActivity.dispatchKeyEvent 在四方向键 ACTION_DOWN 时直接交本引擎，
 *    处理不了也消费——Compose 焦点系统从此收不到方向键，兜底斜跳物理性消失；
 *  - 几何搜索：水平=同行内严格左右最近；垂直=「最近层 + 自缩放层容差」打分
 *    （层容差 = 0.5×两者较小高度，不再需要每屏调 60/100px 魔法数）；
 *  - 滚动找回：按挂靠的 Lazy 容器【瞬时】滚动一个行距（从 layoutInfo 推导，
 *    不再动画轮询），随后重试一次几何搜索；
 *  - 长按节流（65ms/步）+ 滚动进行中防重入——按住方向键即连续平滑移动；
 *  - 特殊语义（tab DOWN 进内容、hero 左右翻页、播放器 seek、浮窗组封锁）
 *    全部改为节点级 navOverride 声明式回调，由引擎统一调用。
 * ===========================================================================
 */

/** 方向常量（与 androidx KeyEvent 键位一一对应） */
const val NAV_LEFT = 0
const val NAV_UP = 1
const val NAV_RIGHT = 2
const val NAV_DOWN = 3

fun navDirOf(keyCode: Int): Int? = when (keyCode) {
    android.view.KeyEvent.KEYCODE_DPAD_LEFT -> NAV_LEFT
    android.view.KeyEvent.KEYCODE_DPAD_UP -> NAV_UP
    android.view.KeyEvent.KEYCODE_DPAD_RIGHT -> NAV_RIGHT
    android.view.KeyEvent.KEYCODE_DPAD_DOWN -> NAV_DOWN
    else -> null
}

// navDx/navDy 移至 NavGeom.kt（同包顶层，几何纯函数与方向编码归一处，供 JVM 单测）

/**
 * 节点挂靠的滚动容器：目标在视口外（未组合）时，引擎按「一个行距」瞬时滚动
 * 再重试几何搜索。行距从 layoutInfo 的相邻项 offset 差推导，天然适配行高变化。
 */
class NavScrollContainer(
    val canForward: () -> Boolean,
    val canBackward: () -> Boolean,
    val stepForward: suspend () -> Boolean,
    val stepBackward: suspend () -> Boolean,
    /** 定向补滚（ensureVisible 用）：正=向下/向右 */
    val scrollBy: suspend (Float) -> Unit = {},
)

/** 相邻两项的 main-axis 间距（含 spacing）= 一行的滚动步长；不足两项退回首项尺寸 */
private fun listPitch(items: List<LazyListItemInfo>): Int {
    if (items.size >= 2) {
        val p = items[1].offset - items[0].offset
        if (p > 0) return p
    }
    return items.firstOrNull()?.size ?: 0
}

/** 网格前两行的顶边距 = 行距（行高+行间距）；单行可见退回最高 cell */
private fun gridRowPitch(items: List<LazyGridItemInfo>): Int {
    val tops = items.map { it.offset.y }.distinct().sorted()
    if (tops.size >= 2) {
        val p = tops[1] - tops[0]
        if (p > 0) return p
    }
    return items.maxOfOrNull { it.size.height } ?: 0
}

fun lazyNavContainer(state: LazyListState): NavScrollContainer = NavScrollContainer(
    canForward = { state.canScrollForward },
    canBackward = { state.canScrollBackward },
    scrollBy = { v -> state.scrollBy(v) },
    stepForward = {
        val info = state.layoutInfo
        val last = info.visibleItemsInfo.lastOrNull() ?: return@NavScrollContainer false
        // 优先「精确露出」：末项被视口裁掉多少就滚多少（+1 确保下一行进入组合区）；
        // 末项完整可见（罕见：下一项恰好未组合）才退回相邻项行距估计。
        // 光标导航#2（2026-09-03 方案B）：步距上限=可见项最大高度——防异常布局
        //（巨 item/负 offset）下单步滚超一行导致「滚穿底」，后续 DOWN 全体 blocked。
        val overshoot = last.offset + last.size - info.viewportEndOffset
        val maxStep = info.visibleItemsInfo.maxOf { it.size }.coerceAtLeast(1)
        val step = minOf(maxStep, if (overshoot > 0) overshoot + 1 else listPitch(info.visibleItemsInfo))
        Log.d("OptimalTV", "stepForward last(idx=${last.index},off=${last.offset},sz=${last.size}) vpEnd=${info.viewportEndOffset} over=$overshoot step=$step")
        if (step <= 0) false else { state.scrollBy(step.toFloat()); true }
    },
    stepBackward = {
        val info = state.layoutInfo
        val first = info.visibleItemsInfo.firstOrNull() ?: return@NavScrollContainer false
        val hidden = info.viewportStartOffset - first.offset
        val maxStep = info.visibleItemsInfo.maxOf { it.size }.coerceAtLeast(1)
        val step = minOf(maxStep, if (hidden > 0) hidden + 1 else listPitch(info.visibleItemsInfo))
        if (step <= 0) false else { state.scrollBy(-step.toFloat()); true }
    },
)

fun lazyGridNavContainer(state: LazyGridState): NavScrollContainer = NavScrollContainer(
    canForward = { state.canScrollForward },
    canBackward = { state.canScrollBackward },
    scrollBy = { v -> state.scrollBy(v) },
    stepForward = {
        val info = state.layoutInfo
        val items = info.visibleItemsInfo
        if (items.isEmpty()) return@NavScrollContainer false
        val overshoot = items.maxOf { it.offset.y + it.size.height } - info.viewportEndOffset
        val step = if (overshoot > 0) overshoot + 1 else gridRowPitch(items)
        if (step <= 0) false else { state.scrollBy(step.toFloat()); true }
    },
    stepBackward = {
        val info = state.layoutInfo
        val items = info.visibleItemsInfo
        if (items.isEmpty()) return@NavScrollContainer false
        val hidden = info.viewportStartOffset - items.minOf { it.offset.y }
        val step = if (hidden > 0) hidden + 1 else gridRowPitch(items)
        if (step <= 0) false else { state.scrollBy(-step.toFloat()); true }
    },
)

/**
 * 普通可滚动容器（verticalScroll/horizontalScroll 的 ScrollState）的导航挂靠。
 * 需求⑰（2026-09-11 播放页侧边栏）：非 lazy 滚动容器里视口外子节点虽已组合，
 * 但 boundsInWindow 上报 Rect.Zero（实测 MuMu/SDK 模拟器，池转储 20 节点全零），
 * 几何搜索永远找不到它们——必须挂靠本容器，让引擎在几何封锁时步进滚动面板、
 * 待滚入视口的节点上报真实 bounds 后重试搜索（launchScrollThenRetry）。
 * 步长取注入窗口高的 1/4（1080p≈270px≈一个 label+chips 区块），无注入时退 480。
 */
fun scrollNavContainer(state: ScrollState): NavScrollContainer {
    fun stepPx(): Float =
        (OtvNav.viewportHeightPx.takeIf { it > 0f } ?: 1440f) / 4f
    return NavScrollContainer(
        canForward = { state.value < state.maxValue },
        canBackward = { state.value > 0 },
        scrollBy = { v -> state.scrollBy(v) },
        stepForward = {
            val left = (state.maxValue - state.value).toFloat()
            if (left <= 0f) false else { state.scrollBy(minOf(stepPx(), left)); true }
        },
        stepBackward = {
            val left = state.value.toFloat()
            if (left <= 0f) false else { state.scrollBy(-minOf(stepPx(), left)); true }
        },
    )
}

/** 节点组合作用域：挂靠的垂直/水平滚动容器（LazyColumn/LazyRow 内容用它声明） */
val LocalNavVertical = androidx.compose.runtime.staticCompositionLocalOf<NavScrollContainer?> { null }
val LocalNavHorizontal = androidx.compose.runtime.staticCompositionLocalOf<NavScrollContainer?> { null }

/** 区域级方向语义覆盖（键盘行/候选行/tab 行等：区域内所有 dpadFocusable 共享） */
val LocalNavOverride = androidx.compose.runtime.staticCompositionLocalOf<((Int) -> Boolean)?> { null }

@Composable
fun ProvideNavScrolls(
    vertical: NavScrollContainer? = null,
    horizontal: NavScrollContainer? = null,
    content: @Composable () -> Unit,
) {
    val v = LocalNavVertical.current
    val h = LocalNavHorizontal.current
    CompositionLocalProvider(
        LocalNavVertical provides (vertical ?: v),
        LocalNavHorizontal provides (horizontal ?: h),
    ) { content() }
}

/**
 * D-pad 焦点修饰符（技术方案 §3.4）：
 * 聚焦态 = 白描边 + 放大；非聚焦态可点击。
 * 节点自注册进 OtvNav 注册表（窗口坐标 + 分组 + 挂靠滚动容器 + 方向覆盖），
 * 方向键统一由 OtvNav 引擎处理。
 */
fun Modifier.dpadFocusable(
    enabled: Boolean = true,
    shape: Shape = RoundedCornerShape(12.dp),
    scaleFocused: Float = 1.06f,
    ringWidth: Dp = 2.dp,
    /** 非空 = 聚焦时画背景胶囊（原版 tab/chip 焦点视觉），不画描边环 */
    focusedBg: Color? = null,
    /** focusedBg 胶囊圆角（默认 12dp；全圆角胶囊型按钮传高度一半） */
    focusedBgRadius: Dp = 12.dp,
    /** 聚焦态回调：白底胶囊时内容需切黑字（原版 sk-key/chip focused 白底黑字） */
    onFocusedChange: ((Boolean) -> Unit)? = null,
    /** 选中即点击（原版 hover-select 语义）：聚焦瞬间自动触发 onClick，调用方自行守卫回环 */
    autoSelect: Boolean = false,
    /** autoSelect 的防重组延迟；生产默认 80ms，测试可拉长以验证失焦取消。 */
    autoSelectDelayMs: Long = 80L,
    /** false = 不画描边环（播放器进度条等自带高亮表达的控件） */
    drawRing: Boolean = true,
    /** true = 悬浮层元素（顶部 tab 栏/联赛筛选）：参与水平行内导航，但不作为垂直移动候选 */
    floating: Boolean = false,
    /** 外部落焦句柄（返回落焦）：与导航注册表共用同一节点 */
    externalFocusRequester: FocusRequester? = null,
    /** 非空 = 节点归入命名分组（播放器浮窗等）：引擎只在本组内找候选，绝不逃逸 */
    navGroup: String? = null,
    /** 节点级方向语义覆盖：返回 true = 已处理（消费），false = 落回几何搜索 */
    navOverride: ((Int) -> Boolean)? = null,
    onClick: () -> Unit,
): Modifier = composed {
    val interaction = remember { MutableInteractionSource() }
    var focused by remember { mutableStateOf(false) }
    val scope = androidx.compose.runtime.rememberCoroutineScope()
    val scaleAnim by animateFloatAsState(
        targetValue = if (focused) scaleFocused else 1f,
        animationSpec = TvInteractionMotion.focusScaleSpec,
        label = "focusScale",
    )
    val bandId = remember { OtvNav.newId() }
    val bandNode = remember(externalFocusRequester) {
        OtvNav.Node(externalFocusRequester ?: FocusRequester(), floating, navGroup)
    }
    // 本实例是否曾持有焦点（跨重组稳定）：销毁时曾是焦点节点 → 触发消失补焦检查
    val wasFocusedOnce = remember { java.util.concurrent.atomic.AtomicBoolean(false) }
    // 组合作用域内同步最新挂靠容器/覆盖回调（lambda 每次重组都会捕获新状态）
    val vertScroll = LocalNavVertical.current
    val horzScroll = LocalNavHorizontal.current
    val regionOverride = LocalNavOverride.current
    SideEffect {
        bandNode.vertScroll = vertScroll
        bandNode.horzScroll = horzScroll
        bandNode.override = navOverride ?: regionOverride
        bandNode.autoSelectNode = autoSelect
    }
    // 2026-09-19 光标乱跑根修：注册表必须始终指向【当前附着】的 bandNode。
    // bandNode 由 remember(externalFocusRequester) 生成——LazyColumn item key
    // （lib-sec-板块名-rN）跨分类同名，切分类/数据刷新后卡片组合被保留但 vod 已换，
    // fr(新id) 变了 → remember 重跑生成新节点挂到修饰符上；注册表若不跟着换，
    // cards[bandId] 就永远停在【从未上过屏的僵尸节点】（bounds=Zero 或冻结在
    // 滚动前 sliver 位置），dispatch 读它做几何起点 → UP 落下一行、横向锁死、
    // DOWN 跳行——「光标乱跑」全家族症状都从这来。
    // SideEffect 每次重组后同步一次（registerCard 无焦点簿记副作用，重复写同一对象零成本）；
    // 真销毁时经 rememberUpdatedState 拿最新节点注销，避免误伤重注册的新节点。
    val latestNode = androidx.compose.runtime.rememberUpdatedState(bandNode)
    SideEffect { OtvNav.registerCard(bandId, latestNode.value) }
    DisposableEffect(bandId) {
        onDispose {
            OtvNav.unregisterCardIf(bandId, latestNode.value)
            // 曾持焦点的节点被销毁（重组/屏过渡）：若焦点无人接管则按锚点补焦，
            // 消灭「切换标签后光标短暂隐形」与「数据加载把焦点甩给邻居」两类现象
            if (wasFocusedOnce.get()) OtvNav.healWhenFocusVanishes(latestNode.value.group)
        }
    }

    this
        .onGloballyPositioned {
            // stamp 与 launchScrollThenRetry 的 stepStamp 同用 uptimeMillis 时钟：
            // 曾混用 currentTimeMillis（墙钟 ~1e12 恒大于 uptime ~1e9）导致
            // 「滚动后重新上报过位置」过滤恒通过——离场冻结节点旧坐标被误判为
            // 前进候选，DOWN 从分类 chip 找回时落到已离场 hero 按钮冻结坐标上，
            // 触发其 animateScrollToItem(0) 页面弹回顶部（v1.18 用户报障根因）
            val nb = it.boundsInWindow()
            val position = it.positionInWindow()
            val layoutSize = Size(it.size.width.toFloat(), it.size.height.toFloat())
            val fullBounds = Rect(
                left = position.x,
                top = position.y,
                right = position.x + layoutSize.width,
                bottom = position.y + layoutSize.height,
            )
            // 2026-09-19 光标乱跑修复：【被裁边的上报不采信】。
            // boundsInWindow 会被 Lazy 容器视口裁边：贴边 sliver 卡（半组合行）上报的
            // 是裁剪后的残条坐标，作为几何候选/搜索起点会算出反向落点。it.size 是
            // 未裁剪布局尺寸——bounds 与之一致才是完整可见上报；残条上报一律丢弃
            //（保留最后一次完整坐标）。贴边 sliver 行因此不作为几何候选，方向移动
            // 走 scroll-reveal 先完整露出再落点，与引擎设计一致。
            OtvNav.reportNodeLayout(
                node = bandNode,
                clippedBounds = nb,
                fullBounds = fullBounds,
                layoutSize = layoutSize,
                stamp = android.os.SystemClock.uptimeMillis(),
            )
            // 滚动会持续位移当前焦点节点——同步刷新自愈锚点，保证漂移恢复坐标新鲜。
            // 当前焦点用未裁剪 fullBounds；候选资格仍由完整可见状态独立控制。
            if (focused) OtvNav.updateAnchor(bandId, bandNode)
        }
        .onFocusEvent {
            if (it.isFocused != focused) {
                // 带窗口坐标与 bandId，便于 logcat 定位焦点去向（boundsInWindow 为布局边界；
                // 同节点重组销毁重建会换 bandId——1ms 内连续 GAINED 换 ID = 重组重建焦点）
                Log.d(TAG, "focus ${if (it.isFocused) "GAINED" else "LOST"}#$bandId @${bandNode.bounds}")
                focused = it.isFocused
                val focusEpoch = if (it.isFocused) {
                    wasFocusedOnce.set(true)
                    OtvNav.onCardFocused(bandId)
                } else {
                    0L
                }
                if (it.isFocused) {
                    // 零尺寸节点稳定器：滚动+重组竞态下 Compose 默认焦点恢复会把焦点落到
                    // 尚未布局的零尺寸节点；120ms 后仍无效则按分组锚点就近自愈。
                    if (!OtvNav.isFocusedNodeUsable(bandNode)) {
                        scope.launch {
                            delay(120)
                            if (focused && OtvNav.isFocusCurrent(bandId, focusEpoch) &&
                                !OtvNav.isFocusedNodeUsable(bandNode)
                            ) {
                                Log.w(TAG, "focus on invalid node — stabilize to nearest")
                                OtvNav.healNearest(bandNode.group)
                            }
                        }
                    }
                }
                if (!it.isFocused) OtvNav.onCardUnfocused(bandId)
                onFocusedChange?.invoke(it.isFocused)
                if (it.isFocused && autoSelect) {
                    // 延迟触发：同帧改状态会引起大重组销毁聚焦节点致焦点丢失。
                    // 只允许仍持有同一焦点事务的节点执行；否则旧 chip 在用户已经
                    // 移走后仍触发 onClick，会把页面和光标强行拉回。
                    scope.launch {
                        delay(autoSelectDelayMs)
                        if (focused && OtvNav.isFocusCurrent(bandId, focusEpoch)) onClick()
                    }
                }
            }
        }
        .focusRequester(bandNode.requester)
        .focusable(enabled = enabled, interactionSource = interaction)
        .clickable(interactionSource = interaction, indication = null, onClick = onClick)
        .scale(scaleAnim)
        .drawWithContent {
            // focusedBg 是「背景」：必须画在内容下层，否则盖住文字
            if (focused) {
                focusedBg?.let {
                    drawRoundRect(
                        color = it,
                        topLeft = Offset.Zero,
                        size = Size(size.width, size.height),
                        cornerRadius = CornerRadius(focusedBgRadius.toPx()),
                    )
                }
            }
            drawContent()
            if (focused && drawRing && !(focusedBg != null && focusedBg.luminance() > 0.5f)) {
                // 白底（亮色聚焦底）元素不画描边环；深色卡片描边环颜色 60% 白
                val strokeW = ringWidth.toPx()
                // 深色衬环：白海报/浅色封面上白色细环不可见（同台标白剪影禁白底聚焦的教训，
                // 2026-09-05 我的页白色海报实测隐形）——先画一圈黑 55% 宽环（半内外），白环叠其上
                drawRoundRect(
                    color = Color.Black.copy(alpha = 0.55f),
                    topLeft = Offset.Zero,
                    size = Size(size.width, size.height),
                    cornerRadius = CornerRadius(12.dp.toPx()),
                    style = Stroke(width = strokeW + 4.dp.toPx()),
                )
                drawRoundRect(
                    color = Color.White.copy(alpha = 0.60f),
                    topLeft = Offset(strokeW, strokeW),
                    size = Size(size.width - strokeW * 2, size.height - strokeW * 2),
                    cornerRadius = CornerRadius(12.dp.toPx()),
                    style = Stroke(width = strokeW),
                )
            }
        }
}

/** 渐变占位海报（图片源不可达时的兜底视觉）。floorMod：hashCode 可能为负，% 会得负索引 */
@Composable
fun PosterPlaceholder(seed: Int, modifier: Modifier = Modifier) {
    val palettes = listOf(
        listOf(Color(0xFF20304A), Color(0xFF0E1626)),
        listOf(Color(0xFF3A2545), Color(0xFF160F1D)),
        listOf(Color(0xFF1E3B33), Color(0xFF0D1A16)),
        listOf(Color(0xFF453225), Color(0xFF1B140E)),
    )
    Box(modifier.background(Brush.linearGradient(palettes[Math.floorMod(seed, palettes.size)])))
}

private const val TAG = "OptimalTV"

/**
 * TV 初始落焦：FocusRequester 的关联目标可能晚于首帧 attach，未就绪时
 * requestFocus 会失败，这里带重试直至落焦成功。
 * enabled=false 时跳过（返回落焦接管）。
 */
@Composable
fun InitialFocusEffect(focusRequester: FocusRequester, tag: String, enabled: Boolean = true) {
    androidx.compose.runtime.LaunchedEffect(focusRequester, enabled) {
        if (!enabled) return@LaunchedEffect
        repeat(60) { attempt ->
            try {
                focusRequester.requestFocus()
                Log.d(TAG, "$tag: initial focus OK (attempt $attempt)")
                return@LaunchedEffect
            } catch (e: IllegalStateException) {
                if (attempt == 0) Log.d(TAG, "$tag: focus not ready, retry: ${e.message}")
            }
            delay(50)
        }
        Log.e(TAG, "$tag: initial focus FAILED after 60 attempts")
    }
}

/**
 * 返回落焦：各屏卡片聚焦时 mark(屏名, 卡key)；返回本屏时按 take(屏名) 找回落焦。
 */
object ReturnFocus {
    private val last = java.util.concurrent.ConcurrentHashMap<String, String>()

    fun mark(screen: String, key: String) { last[screen] = key }

    /** 取回并消费（一次性）。仅「返回本屏」（BACK 从详情/播放回来）应恢复上次落焦；
     *  消费防止旧记忆残留——否则 tab 切回本屏时会被旧 key 劫持：v1.13 实测只要在
     *  影视页聚焦过一次「立即播放」，之后每次经 tab 切回影视页光标都自动跳到该钮。 */
    fun take(screen: String): String? = last.remove(screen)

    /** tab 切换/搜索 chip 等前进入口调用：全部落焦记忆作废，目标屏一律落初始焦点 */
    fun clearAll() { last.clear() }
}

/** 各屏卡片落焦句柄表：key → FocusRequester（与 dpadFocusable 的 externalFocusRequester 配套） */
class FocusRegistry {
    private val map = HashMap<String, FocusRequester>()
    fun fr(key: String): FocusRequester = map.getOrPut(key) { FocusRequester() }
}

/**
 * ===========================================================================
 * 集中式导航引擎本体
 * ===========================================================================
 */
object OtvNav {
    class Node(
        val requester: FocusRequester,
        val floating: Boolean = false,
        val group: String? = null,
        /** 隐藏键宿（播放器无菜单态）：可持焦+响应覆盖，但绝不作为几何候选 */
        val hidden: Boolean = false,
    ) {
        /** 最近一次完整可见的几何候选坐标。 */
        @Volatile var bounds: Rect = Rect.Zero
        @Volatile var stamp: Long = 0
        /** 最近一次布局的未裁剪坐标，只供当前焦点作导航起点/自愈锚点。 */
        @Volatile var focusBounds: Rect = Rect.Zero
        @Volatile var focusStamp: Long = 0
        /** 当前布局是否完整可见；false 时即使保留旧 bounds 也不得进入候选池。 */
        @Volatile var candidateVisible: Boolean = false
        @Volatile var vertScroll: NavScrollContainer? = null
        @Volatile var horzScroll: NavScrollContainer? = null
        @Volatile var override: ((Int) -> Boolean)? = null
        /** 聚焦即点击（hover-select）：自愈/就近恢复的候选必须排除——否则空页面
         *  自愈落到搜索 chip 会 autoSelect 直接劫持跳页（实测 2026-09-01） */
        @Volatile var autoSelectNode: Boolean = false
    }

    private val nextId = java.util.concurrent.atomic.AtomicInteger(1)
    private val cards = java.util.concurrent.ConcurrentHashMap<Int, Node>()

    @Volatile private var focusedCardId = 0
    @Volatile private var lastGroup: String? = null

    /**
     * 焦点事务版本。所有延迟动作都必须带着它执行：一旦焦点已被新的事件接管，
     * 旧的滚动找回/自愈/自动选中立刻失效，不能再把光标抢回旧页面。
     */
    @Volatile private var focusEpoch = 0L

    /**
     * 焦点是否真空中（最后一个焦点事件是 LOST 且之后没有任何 GAINED）。
     * 区分两种「聚焦节点被销毁」：导航离开（新屏节点已 GAINED，focusEmpty=false，勿补焦）
     * vs 重组/屏过渡销毁后焦点凭空消失（focusEmpty=true，须补焦——否则光标隐形到下次按键）。
     */
    @Volatile private var focusEmpty = false

    /** 当前焦点节点所在分组（v1.17 电视页 BACK 分层判定用；null=全局节点） */
    val currentGroup: String? get() = lastGroup

    /** 各分组最近一次【有效】聚焦的自愈锚点（滚动/重组漂移后按坐标就地恢复） */
    private class Anchor(val bounds: Rect)
    private val lastAnchorByGroup = java.util.concurrent.ConcurrentHashMap<String, Anchor>()

    /** 顶部 tab 栏焦点：内容到顶后 UP 的逃逸目标（group=null 的节点才逃逸；浮窗组封锁） */
    @Volatile var topFocus: FocusRequester? = null

    /**
     * v1.16 顶部标签栏自动隐藏（用户需求：往下移动时隐藏顶部标签页，返回顶部才显示）：
     * chromeHideThresholdPx > 0 时启用——聚焦节点顶边越过阈值 = 光标已进入内容区深处，
     * 置 chromeHidden=true；光标回到阈值以上（tab 栏/顶部内容行）恢复 false。
     * 阈值由 MainTabBar(autoHide=true) 进入页面时设置、离开时清除，全局单值。
     */
    @Volatile var chromeHideThresholdPx: Float = 0f
    private val _chromeHidden = kotlinx.coroutines.flow.MutableStateFlow(false)
    val chromeHidden: kotlinx.coroutines.flow.StateFlow<Boolean> = _chromeHidden.asStateFlow()

    fun clearChrome() {
        chromeHideThresholdPx = 0f
        _chromeHidden.value = false
    }

    /** 窗口可视高度（px，ROTATION 感知；MainActivity 从 WindowMetrics 注入，ensureVisible 用） */
    @Volatile var viewportHeightPx: Float = 0f

    @Volatile private var lastMoveAt = 0L
    private val revealSequence = java.util.concurrent.atomic.AtomicLong(0)
    @Volatile private var pendingRevealToken = 0L

    private val scope = CoroutineScope(
        SupervisorJob() + Dispatchers.Main.immediate +
            // 引擎内部任何异常只降级（封锁按键），绝不崩掉整个 TV 应用
            kotlinx.coroutines.CoroutineExceptionHandler { _, e ->
                pendingRevealToken = 0L
                Log.e(TAG, "OtvNav: engine coroutine error (degraded)", e)
            },
    )

    fun newId(): Int = nextId.getAndIncrement()

    /** 仪器测试隔离用：清空引擎全局状态（注册表/焦点/锚点/阈值），每个用例前复位。
     *  仅 androidTest 调用——运行中的 App 绝不可触碰。 */
    internal fun resetForTest() {
        cards.clear()
        focusedCardId = 0
        lastGroup = null
        focusEmpty = false
        lastAnchorByGroup.clear()
        topFocus = null
        chromeHideThresholdPx = 0f
        _chromeHidden.value = false
        viewportHeightPx = 0f
        lastMoveAt = 0L
        pendingRevealToken = 0L
        focusEpoch = 0L
    }

    fun registerCard(id: Int, node: Node) { cards[id] = node }

    /**
     * 同一次布局上报同时维护两条互不污染的状态：
     * - [fullBounds] 是当前节点的未裁剪矩形，可在节点已经持焦时作为方向搜索起点；
     * - [clippedBounds] 只有与布局尺寸一致时，才允许该节点进入几何候选池。
     */
    internal fun reportNodeLayout(
        node: Node,
        clippedBounds: Rect,
        fullBounds: Rect,
        layoutSize: Size,
        stamp: Long,
    ) {
        val focusUsable = stamp > 0L && layoutSize.width >= 2f && layoutSize.height >= 2f &&
            rectUsable(fullBounds) && rectUsable(clippedBounds)
        if (focusUsable) {
            node.focusBounds = fullBounds
            node.focusStamp = stamp
        } else {
            node.focusBounds = Rect.Zero
            node.focusStamp = 0L
        }

        val unclipped = focusUsable &&
            kotlin.math.abs(clippedBounds.width - layoutSize.width) <= 2f &&
            kotlin.math.abs(clippedBounds.height - layoutSize.height) <= 2f
        node.candidateVisible = unclipped
        if (unclipped) {
            node.bounds = fullBounds
            node.stamp = stamp
        }
    }

    fun unregisterCard(id: Int) {
        cards.remove(id)
        if (focusedCardId == id) {
            focusedCardId = 0
            focusEmpty = true
            advanceFocusEpoch()
        }
    }

    /** 仅当注册表里的仍是 [node] 本人才注销（防销毁旧组合时误删重注册的新节点） */
    fun unregisterCardIf(id: Int, node: Node) {
        if (cards.remove(id, node) && focusedCardId == id) {
            focusedCardId = 0
            focusEmpty = true
            advanceFocusEpoch()
        }
    }

    /** 节点失去框架焦点（onFocusEvent LOST）：若之后没有新 GAINED，则焦点真空 */
    fun onCardUnfocused(id: Int) {
        if (focusedCardId == id) {
            focusedCardId = 0
            focusEmpty = true
            advanceFocusEpoch()
        }
    }

    /**
     * 聚焦节点销毁后的补焦兜底（dpadFocusable onDispose 调用）：
     * 100ms 内没有任何节点接管焦点（重组/屏过渡销毁聚焦节点后焦点凭空消失，
     * 光标隐形到下一次按键才被 dispatch 自愈拉回）→ 按分组锚点就近补焦。
     * 导航离开场景（新屏节点已接管焦点，focusEmpty=false）不干预。
     */
    fun healWhenFocusVanishes(group: String?) {
        val expectedEpoch = focusEpoch
        scope.launch {
            repeat(6) {
                delay(50)
                if (!focusEmpty || focusEpoch != expectedEpoch) return@launch
                if (healNearest(group)) return@launch
            }
        }
    }

    /** Returns the epoch that delayed work for this focus event must retain. */
    fun onCardFocused(id: Int): Long {
        focusEmpty = false
        focusedCardId = id
        val epoch = advanceFocusEpoch()
        val n = cards[id] ?: return epoch
        lastGroup = n.group
        val origin = currentNodeOrigin(n)
        if (origin != Rect.Zero) lastAnchorByGroup[n.group ?: ""] = Anchor(origin)
        // v1.16 顶部标签栏自动隐藏联动（阈值 0 = 本屏未启用）
        if (chromeHideThresholdPx > 0f && origin != Rect.Zero) {
            val hide = origin.top > chromeHideThresholdPx
            if (hide != _chromeHidden.value) {
                Log.i(TAG, "chrome hide=$hide (top=${origin.top} > $chromeHideThresholdPx)")
                _chromeHidden.value = hide
            }
        }
        return epoch
    }

    /** True only while [id] still owns the exact focus transaction [epoch]. */
    fun isFocusCurrent(id: Int, epoch: Long): Boolean =
        focusedCardId == id && focusEpoch == epoch && !focusEmpty

    /** 焦点节点布局/滚动位移时刷新自愈锚点（仅当前聚焦节点调用） */
    fun updateAnchor(id: Int, n: Node) {
        val origin = currentNodeOrigin(n)
        if (id == focusedCardId && origin != Rect.Zero) {
            lastAnchorByGroup[n.group ?: ""] = Anchor(origin)
        }
    }

    private fun rectUsable(rect: Rect): Boolean =
        rect != Rect.Zero && rect.left.isFinite() && rect.top.isFinite() &&
            rect.right.isFinite() && rect.bottom.isFinite() &&
            rect.width >= 2f && rect.height >= 2f

    internal fun isNavigationCandidate(n: Node): Boolean =
        n.candidateVisible && n.stamp > 0L && rectUsable(n.bounds)

    internal fun currentNodeOrigin(n: Node): Rect = when {
        n.focusStamp > 0L && rectUsable(n.focusBounds) -> n.focusBounds
        isNavigationCandidate(n) -> n.bounds
        else -> Rect.Zero
    }

    /** 当前已持焦节点是否有可用起点；不代表它能作为其他节点的搜索候选。 */
    internal fun isFocusedNodeUsable(n: Node): Boolean = currentNodeOrigin(n) != Rect.Zero

    /**
     * 统一入口（MainActivity.dispatchKeyEvent 调用）。
     * 返回值恒 true：四方向键一律由引擎消费，绝不放行给 Compose 兜底几何搜索。
     */
    fun dispatch(dir: Int, repeatCount: Int): Boolean {
        if (pendingRevealToken != 0L) return true
        if (!TvInteractionMotion.shouldProcessDirection(
                repeatCount = repeatCount,
                elapsedMs = android.os.SystemClock.uptimeMillis() - lastMoveAt,
            )
        ) return true

        val curPre = cards[focusedCardId]
        var cur = curPre
        if (cur == null || (!isFocusedNodeUsable(cur) && !cur.hidden)) {
            // 焦点漂移自愈（页面切换/重组竞态）：恢复后【继续走 override 检查】——
            // 搜索 chip 的 DOWN=定向进内容等语义在过渡期同样必须生效。
            // 隐藏键宿（播放器 1dp sink）不参与几何、不受尺寸校验，直接可用。
            cur = healAndMove(dir)
            if (cur == null) {
                lastMoveAt = android.os.SystemClock.uptimeMillis()
                return true
            }
        }

        // 1) 节点/区域级方向语义（tab DOWN 进内容、hero 翻页、播放器 seek、浮窗封锁…）
        cur.override?.let { ov ->
            if (ov(dir)) {
                lastMoveAt = android.os.SystemClock.uptimeMillis()
                return true
            }
        }

        // 2) 确定性几何搜索（组内、浮窗排除、自缩放层容差）
        // 2026-09-19 光标乱跑修复：搜索起点优先用【本组自愈锚点】而非 cur.bounds。
        // 锚点只在【聚焦节点的真实上报】时刷新（onCardFocused/updateAnchor），
        // 构造上恒等于当前焦点节点最后一次聚焦位置——即使注册 bounds 因 Lazy 容器
        // 裁边/节点置换出现异常，锚点仍可靠（实测影视页片库首行卡 UP 反向落点根因）。
        val origin = lastAnchorByGroup[cur.group ?: ""]?.bounds ?: currentNodeOrigin(cur)
        val target = searchGeom(origin, dir, cur.group, exclude = cur)
        if (target != null && focusNode(target)) {
            ensureVisible(target, dir)
            lastMoveAt = android.os.SystemClock.uptimeMillis()
            return true
        }

        // 3) 目标未组合：按挂靠容器瞬时滚一行再重试
        val cont = if (navDx(dir) != 0) cur.horzScroll else cur.vertScroll
        if (cont != null && canStep(cont, dir)) {
            val sourceId = cards.entries.firstOrNull { it.value === cur }?.key ?: focusedCardId
            launchScrollThenRetry(sourceId, cur, cont, dir)
            lastMoveAt = android.os.SystemClock.uptimeMillis()
            return true
        }

        // 4) UP 到顶逃逸：仅全局节点回顶部 tab 栏；浮窗组/其余方向封锁（消费不放行）
        if (dir == NAV_UP && cur.group == null) {
            topFocus?.let { fr -> runCatching { fr.requestFocus() } }
        } else if (repeatCount == 0) {
            val c = if (navDx(dir) != 0) cur.horzScroll else cur.vertScroll
            Log.d(TAG, "OtvNav: blocked dir=$dir cur=${cur.bounds} group=${cur.group} canStep=${c?.let { canStep(it, dir) }}")
        }
        lastMoveAt = android.os.SystemClock.uptimeMillis()
        return true
    }

    private fun canStep(cont: NavScrollContainer, dir: Int): Boolean =
        if (dir == NAV_DOWN || dir == NAV_RIGHT) cont.canForward() else cont.canBackward()

    /** 瞬时滚动一行（滚动即组合出目标行）→ 等一帧布局 → 重试几何搜索 */
    private fun launchScrollThenRetry(sourceId: Int, cur: Node, cont: NavScrollContainer, dir: Int) {
        val expectedEpoch = focusEpoch
        val token = revealSequence.incrementAndGet()
        pendingRevealToken = token
        scope.launch {
            try {
                repeat(3) {
                    if (!isRevealCurrent(token, sourceId, expectedEpoch)) return@launch
                    val stepStamp = android.os.SystemClock.uptimeMillis()
                    val stepped = if (dir == NAV_DOWN || dir == NAV_RIGHT) cont.stepForward() else cont.stepBackward()
                    Log.d(TAG, "scroll-reveal#${it + 1} dir=$dir stepped=$stepped stepStamp=$stepStamp")
                    if (!stepped) return@launch
                    // 预留约两帧让滚入视口的节点完成 measure/layout；较旧 50ms 等待
                    // 缩短 36%，同时保留异步窗口，允许页面/浮层的新焦点事务接管。
                    delay(TvInteractionMotion.SCROLL_REVEAL_LAYOUT_WAIT_MS)
                    if (!isRevealCurrent(token, sourceId, expectedEpoch)) return@launch
                    val fresh = cards[sourceId]?.takeIf { isNavigationCandidate(it) } ?: cur
                    // 只认本次滚动后【重新上报过位置】的节点：随滚动离场但尚未注销的节点
                    // 带着旧 bounds，会被几何搜索误判成前进方向候选——实测 DOWN 从分类
                    // chip 找回时落到已离场的 hero 按钮旧坐标上（v1.18 修复）
                    // 2026-09-19：起点同样走锚点（防幽灵上报回写，见 dispatch 注释）
                    val retryOrigin = lastAnchorByGroup[fresh.group ?: cur.group ?: ""]?.bounds ?: fresh.bounds
                    val t = searchGeom(retryOrigin, dir, fresh.group ?: cur.group, exclude = fresh)
                    Log.d(TAG, "scroll-reveal#${it + 1} fresh=${fresh.bounds} hit=${t?.bounds} stamp=${t?.stamp} ok=${t != null && t.stamp >= stepStamp}")
                    if (t != null && t.stamp >= stepStamp && focusNode(t)) return@launch
                }
                Log.d(TAG, "OtvNav: scroll-reveal exhausted dir=$dir")
            } finally {
                if (pendingRevealToken == token) pendingRevealToken = 0L
            }
        }
    }

    private fun focusNode(n: Node): Boolean = try {
        // 当前 Compose BOM 的 requestFocus() 返回 Unit；真正的确认来自紧随其后的
        // onFocusEvent GAINED（它会推进 focusEpoch，令旧异步事务失效）。
        n.requester.requestFocus()
        true
    } catch (e: IllegalStateException) {
        false
    }

    /**
     * 落焦后确保目标完整可见：Compose bringIntoView 只对「不可见」节点滚动，
     * 部分露出（如屏底只露 38px 的卡片，焦点环被裁）不会触发——这里按挂靠容器
     * 定向补滚露出余量。TV 应用窗口全屏，屏幕高即窗口高。
     */
    private fun ensureVisible(n: Node, dir: Int) {
        val cont = n.vertScroll ?: return
        val b = n.bounds
        // 优先用注入的窗口高（旋转感知）；退化用 Resources（自然方向，可能偏大——只影响补滚量）
        val screenH = if (viewportHeightPx > 0f) viewportHeightPx
        else android.content.res.Resources.getSystem().displayMetrics.heightPixels.toFloat()
        val overBottom = b.bottom - screenH + 16f
        val overTop = 16f - b.top
        when {
            overBottom > 0 && cont.canForward() -> scope.launch { cont.scrollBy(overBottom) }
            overTop > 0 && cont.canBackward() -> scope.launch { cont.scrollBy(-overTop) }
        }
    }

    /**
     * 焦点漂移自愈：当前焦点节点无效（零尺寸/已销毁）时，以分组最近有效焦点的
     * 坐标为锚做方向搜索；无锚则就近自愈；再不行回顶部 tab 栏。
     * 返回恢复后的节点（供 dispatch 继续 override 检查），失败返回 null。
     */
    private fun healAndMove(dir: Int): Node? {
        val group = lastGroup
        val anchor = lastAnchorByGroup[group ?: ""]
        Log.w(TAG, "OtvNav: focus node invalid — self-heal dir=$dir group=$group anchor=${anchor?.bounds}")
        if (anchor != null) {
            searchGeom(anchor.bounds, dir, group, exclude = null, excludeAutoSelect = true)?.let { if (focusNode(it)) return it }
            nearestValid(group, anchor.bounds)?.let { if (focusNode(it)) return it }
        }
        topFocus?.let { fr ->
            if (runCatching { fr.requestFocus() }.isSuccess) {
                return cards.entries.firstOrNull { it.value.requester === fr }?.value
            }
        }
        return null
    }

    /** 无方向自愈：分组内离锚点最近的有效节点 */
    fun healNearest(group: String? = null): Boolean {
        val anchor = lastAnchorByGroup[group ?: ""]?.bounds ?: run {
            val f = topFocus ?: return false
            return runCatching { f.requestFocus() }.isSuccess
        }
        val target = nearestValid(group, anchor) ?: return false
        return focusNode(target)
    }

    private fun nearestValid(group: String?, anchor: Rect): Node? =
        cards.entries.asSequence()
            .filter { (_, n) -> isNavigationCandidate(n) && !n.hidden && !n.autoSelectNode && (group == null || n.group == group) }
            .map { entry ->
                val dx = entry.value.bounds.center.x - anchor.center.x
                val dy = entry.value.bounds.center.y - anchor.center.y
                entry to (dx * dx + dy * dy)
            }
            .minWithOrNull(compareBy({ it.second }, { it.first.key }))
            ?.first
            ?.value

    private fun advanceFocusEpoch(): Long {
        focusEpoch += 1L
        // 已换焦点时不应继续吞按键；旧协程还会靠 epoch/token 校验自行退出。
        pendingRevealToken = 0L
        return focusEpoch
    }

    private fun isRevealCurrent(token: Long, sourceId: Int, expectedEpoch: Long): Boolean =
        pendingRevealToken == token && focusEpoch == expectedEpoch &&
            (sourceId == 0 || focusedCardId == sourceId)

    // sameRow / searchGeom 的几何规则已抽至 NavGeom（纯函数，可 JVM 单测）；
    // 这里只做注册表过滤（valid/hidden/分组/exclude/autoSelect 排除）再委托。
    private fun searchGeom(cb: Rect, dir: Int, group: String?, exclude: Node?, excludeAutoSelect: Boolean = false): Node? {
        // ConcurrentHashMap 的迭代是弱一致的；先取一次快照再按 id 排序，并把 id 传到
        // NavGeom 做评分平局决胜，确保每一次按键的目标与枚举顺序无关。
        val pool = cards.entries.asSequence()
            .filter { (_, n) -> n !== exclude && isNavigationCandidate(n) && !n.hidden }
            .filter { (_, n) -> !excludeAutoSelect || !n.autoSelectNode }
            .filter { (_, n) -> n.group == group }
            .map { entry -> entry.key to entry.value }
            .toList()
            .sortedBy { it.first }
            .map { (id, n) -> n to NavGeom.Cand(n.bounds, n.floating, id) }
        return NavGeom.search(cb, dir, pool.map { it.second })
            ?.let { hit -> pool.firstOrNull { it.second.stableKey == hit.stableKey }?.first }
    }
}

/**
 * 隐藏键宿：持有焦点但不参与几何导航（播放器根键宿/菜单隐藏态 sink）。
 * 方向键到达时先调 override（seek/唤出菜单等），返回 false 落回引擎几何搜索。
 */
fun Modifier.navSink(
    requester: FocusRequester? = null,
    override: ((Int) -> Boolean)? = null,
): Modifier = composed {
    val sinkId = remember { OtvNav.newId() }
    val fr = remember { requester ?: FocusRequester() }
    val node = remember { OtvNav.Node(fr, hidden = true) }
    SideEffect { node.override = override }
    DisposableEffect(sinkId) {
        OtvNav.registerCard(sinkId, node)
        onDispose { OtvNav.unregisterCard(sinkId) }
    }
    this
        .onGloballyPositioned {
            node.bounds = it.boundsInWindow(); node.stamp = android.os.SystemClock.uptimeMillis()
        }
        .onFocusEvent {
            if (it.isFocused) {
                Log.d(TAG, "focus GAINED(sink) @${node.bounds}")
                OtvNav.onCardFocused(sinkId)
            }
        }
        .focusRequester(fr)
        .focusable()
}

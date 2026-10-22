package com.qiubo.optimaltv.ui.components

import androidx.compose.foundation.background
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.res.painterResource
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import androidx.navigation.NavController
import androidx.navigation.NavGraph.Companion.findStartDestination
import com.qiubo.optimaltv.R
import com.qiubo.optimaltv.ui.components.OtvNav
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.launch

/**
 * 顶部 Tab Bar（原版 #topbar + #tabbar 1:1）：
 * 顶栏 = 150 高渐变遮罩（rgba(0,0,0,.6)→.35@55%→0，即原版 topbar 渐变，blur 由 glass 叠加）；
 * 胶囊容器 h68 r34 bg rgba(30,30,30,.5) px10 gap6；tab h56 px26 字29 粗体：
 * 未选 白60% 无底；所在页 = 黑36%胶囊 + 白字（.tab.active::before）；聚焦 = 黑60%胶囊 + 白字（.tab.focused）。
 * 末位搜索 chip 111×56 r35 图标 #999，聚焦黑60%胶囊 + 图标变白。标准 TV：方向键移光标，OK 确认。
 */
data class TabItem(val key: String, val label: String)

val MAIN_TABS = listOf(
    TabItem("live", "足球直播"),
)

/** Tab 间导航统一入口：popUpTo 起点页 + launchSingleTop，反复横跳不撑爆返回栈。
 *  tab 切换是「新进入」而非「返回」——先清空 ReturnFocus 记忆，防止上次离开时的
 *  卡位（如 hero 立即播放）在切回时把光标自动拽进内容区（v1.13 实测缺陷 #7）。 */
fun navigateToTab(nav: NavController, key: String) {
    ReturnFocus.clearAll()
    nav.navigate(key) {
        popUpTo(nav.graph.findStartDestination().id) { inclusive = false }
        launchSingleTop = true
    }
}

@Composable
fun MainTabBar(
    currentKey: String,
    onSelect: (String) -> Unit,
    initialFocus: FocusRequester? = null,
    /** 顶部毛玻璃层（原版 #topbar backdrop-filter:blur）：画在渐变之上、brand/胶囊之下，全宽 150 高 */
    glass: (@Composable () -> Unit)? = null,
    /** 非空 = tab 上按 DOWN 定向到内容区首焦点（几何搜索会被水平重叠的 chips 抢走） */
    contentDownFocus: FocusRequester? = null,
    /** 深滚后内容首目标（hero）被 LazyColumn 回收、requestFocus 抛异常时的兜底：
     *  各屏传「滚回顶部」，随后自动重试落焦（loading 中按 DOWN 也能在内容就绪后落焦） */
    contentDownScrollTop: (suspend () -> Unit)? = null,
    /** false = 纯展示：tab 不可聚焦。 */
    interactive: Boolean = true,
    /** 任一 tab 聚焦状态回调（供各屏 BackHandler 判定「已在顶部 tab 栏」再退出，需求 #2） */
    onTabFocused: ((Boolean) -> Unit)? = null,
    /** v1.16 自动隐藏（用户需求：光标下移进内容区隐藏顶栏，回顶部才显示）：
     *  true = 聚焦节点顶边越过阈值即淡出上移本栏；光标回顶自动淡入。 */
    autoHide: Boolean = false,
    /** 内容滚动到顶后仍需隐藏顶栏时使用（如海报行滚到视口顶部）。 */
    contentKeepsChromeHidden: Boolean = false,
) {
    val s = rememberUiScale()
    // 进入页面挂阈值、离开时清除（OtvNav 全局单值；各带栏页面均传 autoHide=true）。
    // 阈值为物理 px（OtvNav 节点 bounds 为窗口物理坐标）。
    // 2026-09-19：300 → 200 设计px。原值是从「滚动驱动」的移动端口径直接搬来的，
    // 在 TV 版（焦点驱动）上等于要求「光标下移到 y>300」才收起——电视页的频道网格
    // 第一行顶边只有 ~240（chips 行 150），于是下移进网格时顶栏纹丝不动（用户报障）。
    // 200 设计px 落在 chips 行下方、网格首行上方：光标进内容区即隐藏、回到 chips/tab 恢复。
    val tabCtx = androidx.compose.ui.platform.LocalContext.current
    if (autoHide) {
        androidx.compose.runtime.DisposableEffect(tabCtx) {
            OtvNav.chromeHideThresholdPx = 200f * tabCtx.resources.displayMetrics.widthPixels / 1920f
            onDispose { OtvNav.clearChrome() }
        }
    }
    val chromeHidden by OtvNav.chromeHidden.collectAsState()
    val shouldHideChrome = autoHide && (chromeHidden || contentKeepsChromeHidden)
    val chromeAlpha by androidx.compose.animation.core.animateFloatAsState(
        targetValue = if (shouldHideChrome) 0f else 1f,
        animationSpec = androidx.compose.animation.core.tween(220),
        label = "tabChromeAlpha",
    )
    val chromeTy by androidx.compose.animation.core.animateFloatAsState(
        targetValue = if (shouldHideChrome) -90f else 0f,
        animationSpec = androidx.compose.animation.core.tween(220),
        label = "tabChromeTy",
    )
    Box(
        Modifier.fillMaxWidth().graphicsLayer {
            alpha = chromeAlpha
            translationY = chromeTy
        },
    ) {
        // 顶栏渐变遮罩（原版 #topbar：黑 .6→.35@55%→0 + mask 72% 后淡出）
        Column(Modifier.fillMaxWidth().height(150f.sx(s))) {
            Box(
                Modifier
                    .fillMaxWidth()
                    .height(150f.sx(s))
                    .background(
                        Brush.verticalGradient(
                            0f to Color(0x99000000),
                            0.55f to Color(0x59000000),
                            1f to Color(0x00000000),
                        )
                    ),
            )
        }
        glass?.invoke()
        // 单标签导航保持紧凑，不重复显示品牌 logo。
        // v1.13：行内左右/UP 封锁/DOWN 定向全部由 OtvNav 引擎处理——
        // tab 节点 navOverride：DOWN = 进内容区首焦点（几何搜索会被水平重叠的 chips 抢走）；
        // UP 一律封锁（顶栏之上无区域，放行会被兜底甩进下方内容）
        val downScope = androidx.compose.runtime.rememberCoroutineScope()
        val tabDir: (Int) -> Boolean = { dir ->
            when (dir) {
                com.qiubo.optimaltv.ui.components.NAV_DOWN -> {
                    if (contentDownFocus != null) {
                        // 防闪退：内容区首目标（hero 钮）可能尚未组合（loading）或已被
                        // LazyColumn 深滚回收，requestFocus 会抛——先滚回顶部再带重试落焦
                        val ok = runCatching { contentDownFocus.requestFocus() }.isSuccess
                        if (!ok && contentDownScrollTop != null) {
                            downScope.launch {
                                contentDownScrollTop()
                                repeat(20) {
                                    if (runCatching { contentDownFocus.requestFocus() }.isSuccess) return@launch
                                    kotlinx.coroutines.delay(50)
                                }
                            }
                        }
                    }
                    true
                }
                com.qiubo.optimaltv.ui.components.NAV_UP -> true
                else -> false
            }
        }
        androidx.compose.runtime.CompositionLocalProvider(
            com.qiubo.optimaltv.ui.components.LocalNavOverride provides tabDir,
        ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(6f.sx(s)),
            modifier = Modifier
                .align(Alignment.TopCenter)
                .padding(top = 48f.sx(s))
                .height(68f.sx(s))
                .background(OtvColors.TabBarBg, RoundedCornerShape(34f.sx(s)))
                .padding(horizontal = 10f.sx(s)),
        ) {
            // 注册当前 tab 为全局「顶部焦点」：内容区按 UP 到顶后回到此 tab 栏（需求 #7）。
            // 传 null（搜索页自行注册 topFocus 回软键盘）时不覆盖，需求 搜索#1
            LaunchedEffect(currentKey) { if (initialFocus != null) OtvNav.topFocus = initialFocus }
            MAIN_TABS.forEach { tab ->
                val selected = tab.key == currentKey
                var focused by remember { mutableStateOf(false) }
                // 无所在标签页（播放器等独立页面，currentKey 空串）：initialFocus 锚定
                // 首个 tab 作为 UP 逃逸落点。落点必须关 autoSelect——否则聚焦 80ms 后
                // hover-select 直接劫持跳页，用户永远停不在标签栏上左右选页
                val anchored = initialFocus != null &&
                    (tab.key == currentKey || (currentKey.isBlank() && tab.key == MAIN_TABS.first().key))
                val focusMod = if (initialFocus != null && anchored) {
                    Modifier.focusRequester(initialFocus)
                } else Modifier
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .then(focusMod)
                        .then(
                            if (interactive) {
                                Modifier.dpadFocusable(
                                    scaleFocused = 1f,
                                    // 聚焦胶囊走 focusedBg（黑60%，原版 .tab.focused::before）；focusedBg 非空则无白描边环
                                    focusedBg = Color(0x99000000),
                                    focusedBgRadius = 28f.sx(s),
                                    floating = true,
                                    onFocusedChange = {
                                        focused = it
                                        onTabFocused?.invoke(it)
                                    },
                                    // 选中即点击（原版 hover-select）：移动到即切页；当前页 tab 与锚定落点不触发防落焦回环
                                    autoSelect = !selected && !anchored,
                                ) { onSelect(tab.key) }
                            } else Modifier,
                        )
                        .background(
                            // 所在页未聚焦 = 黑36%胶囊（原版 .tab.active::before）；聚焦胶囊由 focusedBg 画
                            if (selected && !focused) Color(0x5C000000) else Color.Transparent,
                            RoundedCornerShape(28f.sx(s)),
                        )
                        .padding(horizontal = 26f.sx(s))
                        .height(56f.sx(s)),
                ) {
                    Text(
                        tab.label,
                        fontSize = 29f.sxs(s),
                        fontWeight = FontWeight.Bold,
                        lineHeight = 34f.sxs(s),
                        color = if (selected || focused) OtvColors.White else OtvColors.White60,
                    )
                }
            }
        }
        } // CompositionLocalProvider(LocalNavOverride provides tabDir)
    }
}

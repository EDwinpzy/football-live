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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.navigation.NavController
import androidx.navigation.NavGraph.Companion.findStartDestination
import com.qiubo.optimaltv.R
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs

/**
 * 顶部 Tab Bar（v1.18 移动版与 TV 版 1:1 同款，原版 #topbar + #tabbar 复刻）：
 * 顶栏 = 150 高渐变遮罩（rgba(0,0,0,.6)→.35@55%→0）；胶囊容器 h68 r34 bg rgba(30,30,30,.5)
 * px10 gap6；tab h56 px26 字29 粗体：未选 白60% 无底；所在页 = 黑36%胶囊 + 白字。
 * 末位搜索 chip 111×56 r35 图标 #999。与 TV 版唯一差异：交互由 D-pad 聚焦改为触控点按
 * （无聚焦胶囊态；所在页胶囊即「当前页」视觉锚）。
 */
data class TabItem(val key: String, val label: String)

val MAIN_TABS = listOf(
    TabItem("live", "足球直播"),
)

/** Tab 间导航统一入口（与 TV 版同语义）：popUpTo 起点页 + launchSingleTop，反复横跳不撑爆返回栈 */
fun navigateToTab(nav: NavController, key: String) {
    nav.navigate(key) {
        popUpTo(nav.graph.findStartDestination().id) { inclusive = false }
        launchSingleTop = true
    }
}

@Composable
fun MainTabBar(
    currentKey: String,
    onSelect: (String) -> Unit,
    /** 触控版 auto-hide：内容区滚离顶部时由各屏传入 true，顶栏淡出上移；回顶自动淡入 */
    hidden: Boolean = false,
    /** 纯展示时不响应触摸。 */
    interactive: Boolean = true,
) {
    val s = rememberUiScale()
    // 隐藏态必须同步撤销语义点击/触摸命中，不能只把 alpha 变成 0；
    // 否则透明顶栏仍会截获内容区点击。
    val acceptsInput = interactive && !hidden
    val chromeAlpha = animateFloatAsState(
        targetValue = if (hidden) 0f else 1f,
        animationSpec = tween(220),
        label = "tabChromeAlpha",
    )
    val chromeTy = animateFloatAsState(
        targetValue = if (hidden) -90f else 0f,
        animationSpec = tween(220),
        label = "tabChromeTy",
    )
    Box(
        Modifier
            .fillMaxWidth()
            .testTag(MobileUiTags.MainTabBar)
            .graphicsLayer {
                alpha = chromeAlpha.value
                translationY = chromeTy.value
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
            MAIN_TABS.forEach { tab ->
                val selected = tab.key == currentKey
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .then(
                            if (acceptsInput) {
                                Modifier
                                    .clip(RoundedCornerShape(28f.sx(s)))
                                    .tapCard { onSelect(tab.key) }
                            } else Modifier
                        )
                        .testTag(MobileUiTags.mainTab(tab.key))
                        .semantics { this.selected = selected; role = Role.Tab }
                        .background(
                            // 所在页 = 黑36%胶囊（原版 .tab.active::before）
                            if (selected) Color(0x5C000000) else Color.Transparent,
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
                        color = if (selected) OtvColors.White else OtvColors.White60,
                    )
                }
            }
        }
    }
}

/**
 * 内容区是否已经离开绝对顶部。
 *
 * 顶部标签栏的隐藏不再等待固定像素阈值：只要首项有任何滚动偏移，
 * 或首个可见项已经不是第 0 项，就视为页面已离开顶部。
 */
fun isScrolledFromTop(firstVisibleItemIndex: Int, firstVisibleItemScrollOffset: Int): Boolean =
    firstVisibleItemIndex > 0 || firstVisibleItemScrollOffset > 0

package com.qiubo.optimaltv.ui.paywall

import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.navigation.NavController
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.qiubo.optimaltv.license.LicenseManager
import com.qiubo.optimaltv.license.LicenseState
import com.qiubo.optimaltv.ui.components.InitialFocusEffect
import com.qiubo.optimaltv.ui.components.MainTabBar
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.components.navigateToTab
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs

/**
 * v1.17 会员页（公测版，方案 §4.4）：
 * 月/季/年/终身四张 Apple TV 风格焦点卡；聚焦卡片右侧出购买二维码
 * （ZXing 生成，指向发卡平台商品页，手机扫码付款自动收货）；
 * 底部「输入卡密激活」入口。仅由用户主动打开，授权状态不拦截主功能。
 * 套餐目录/二维码/状态行在 PaywallShared.kt（与移动版共享同步）。
 * 需求②（2026-09-04）：「我的」页未开通态与会员页统一——FavoritesScreen 未激活时
 * 整页复用本页；tabKey 透传让嵌入态标签栏仍高亮「我的」。
 */
@Composable
fun PaywallScreen(nav: NavController, tabKey: String = "") {
    val s = rememberUiScale()
    val st by LicenseManager.state.collectAsStateWithLifecycle()

    BackHandler { nav.popBackStack() }

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        // 套餐首卡焦点句柄：PaywallOffers 首卡落焦 + 未激活态 tab DOWN 定向共用
        val firstCard = remember { FocusRequester() }
        Column(Modifier.fillMaxSize().padding(top = 120f.sx(s))) {
            // ---- 标题 + 状态 ----
            Row(verticalAlignment = Alignment.Bottom, modifier = Modifier.padding(start = 96f.sx(s))) {
                Text("会员", color = OtvColors.White, fontSize = 52f.sxs(s), fontWeight = FontWeight.Bold)
                Spacer(Modifier.width(28f.sx(s)))
                Text(
                    statusLine(st),
                    color = OtvColors.White60, fontSize = 26f.sxs(s),
                    modifier = Modifier.padding(bottom = 8f.sx(s)),
                )
            }
            Spacer(Modifier.height(12f.sx(s)))
            Text(
                "开通会员观看全部直播、足球与影视内容 · 一码可绑 2 台设备",
                color = OtvColors.White50, fontSize = 24f.sxs(s),
                modifier = Modifier.padding(start = 96f.sx(s)),
            )

            Spacer(Modifier.height(64f.sx(s)))

            // ---- 四张套餐卡 + 右侧购买二维码 + 输码入口（复用组件） ----
            InitialFocusEffect(firstCard, "paywall-first", enabled = true)
            PaywallOffers(
                nav = nav,
                s = s,
                firstCard = firstCard,
                modifier = Modifier.padding(start = 96f.sx(s)),
            )
        }
        // 需求 会员#8：未激活时标签栏可交互——光标可 UP 上到顶部标签栏（UP 逃逸锚定
        // 首个 tab，落点停驻可左右选页），hover/OK 点击均可切换；DOWN 回套餐首卡。
        // 已激活（卡密兑换续期）保持纯展示二级页规范（与搜索页一致防误切）
        val tabFocus = remember { FocusRequester() }
        val notActivated = st is LicenseState.NotActivated
        MainTabBar(
            tabKey, { key -> navigateToTab(nav, key) }, { nav.navigate("search") },
            if (notActivated) tabFocus else null,
            contentDownFocus = firstCard,
            interactive = notActivated,
        )
    }
}

/**
 * 会员购买区（2026-09-03 用户需求：没激活时「我的」页直接显示会员充值界面）：
 * 月/季/年/终身四张套餐卡 + 聚焦卡片右侧展开购买二维码 + 「输入卡密激活」入口。
 * PaywallScreen 全屏页与 FavoritesScreen 未激活嵌入态共用；左右留白由调用方
 * 通过 modifier 控制（全屏页 start=96，「我的」页与套餐卡对齐自行给足可容纳
 * 二维码展开的宽度）。firstCard：套餐首卡焦点句柄，调用方控制首焦点落点。
 */
@Composable
fun PaywallOffers(nav: NavController, s: Float, firstCard: FocusRequester?, modifier: Modifier = Modifier) {
    var focusedPlan by remember { mutableStateOf<String?>(null) }
    Column(modifier) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Row(horizontalArrangement = Arrangement.spacedBy(28f.sx(s))) {
                PLANS.forEachIndexed { i, p ->
                    PlanCard(
                        p, s,
                        focus = if (i == 0) firstCard else null,
                        onFocused = { focusedPlan = if (it) p.plan else focusedPlan },
                    )
                }
            }
            // 选中套餐 → 右侧二维码购买面板（方案 §4.4）
            AnimatedVisibility(
                visible = focusedPlan != null,
                enter = fadeIn(),
                exit = fadeOut(),
            ) {
                val p = PLANS.firstOrNull { it.plan == focusedPlan } ?: PLANS[0]
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(start = 64f.sx(s))) {
                    QrPanel(p, s)
                }
            }
        }

        Spacer(Modifier.height(44f.sx(s)))

        // ---- 输入卡密激活 ----
        Row(
            horizontalArrangement = Arrangement.spacedBy(18f.sx(s)),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            val activateBtn = remember { FocusRequester() }
            var actFocused by remember { mutableStateOf(false) }
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier
                    .focusRequester(activateBtn)
                    .dpadFocusable(
                        scaleFocused = 1.04f,
                        focusedBg = OtvColors.White,
                        focusedBgRadius = 30f.sx(s),
                        onFocusedChange = { actFocused = it },
                    ) { nav.navigate("activate") }
                    .background(Color(0x24FFFFFF), RoundedCornerShape(30f.sx(s)))
                    .padding(horizontal = 54f.sx(s), vertical = 24f.sx(s)),
            ) {
                Text(
                    "输入卡密激活",
                    color = if (actFocused) OtvColors.Bg else OtvColors.White,
                    fontSize = 30f.sxs(s), fontWeight = FontWeight.SemiBold,
                )
            }
            Spacer(Modifier.width(30f.sx(s)))
            Text(
                "购买后获得 10 位卡密（OTV-XXXXX-XXXXX）",
                color = OtvColors.White50, fontSize = 24f.sxs(s),
            )
        }
    }
}

/** 套餐卡（Apple TV 风格：深色卡身，聚焦白底反色 + 微放大） */
@Composable
private fun PlanCard(
    p: PlanUi,
    s: Float,
    focus: FocusRequester? = null,
    onFocused: (Boolean) -> Unit = {},
) {
    var f by remember { mutableStateOf(false) }
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        modifier = Modifier
            .then(if (focus != null) Modifier.focusRequester(focus) else Modifier)
            .dpadFocusable(
                scaleFocused = 1.05f,
                focusedBg = OtvColors.White,
                focusedBgRadius = 20f.sx(s),
                onFocusedChange = { f = it; onFocused(it) },
            ) { }
            // 深色底是卡片静态底：聚焦时必须让位（透明），否则会盖住 focusedBg 白底、
            // 反色后的深色文字直接糊进深底（TabBar「聚焦胶囊由 focusedBg 画」同款惯例）
            .background(if (f) Color.Transparent else Color(0xFF1C1C22), RoundedCornerShape(20f.sx(s)))
            .width(286f.sx(s))
            .height(330f.sx(s))
            .padding(top = 46f.sx(s)),
    ) {
        Text(p.name, color = if (f) OtvColors.Bg else OtvColors.White, fontSize = 30f.sxs(s), fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(22f.sx(s)))
        Text(p.price, color = if (f) OtvColors.Bg else OtvColors.White, fontSize = 52f.sxs(s), fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(18f.sx(s)))
        Text(p.sub, color = if (f) Color(0x990E0E0F) else OtvColors.White50, fontSize = 24f.sxs(s))
    }
}

/** 右侧购买面板：白底二维码（电视上扫码对比度最佳）+ 套餐说明 */
@Composable
private fun QrPanel(p: PlanUi, s: Float) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        QrCode(
            content = PurchaseLinks.forPlan(p.plan),
            size = 330f.sx(s),
            modifier = Modifier
                .background(Color.White, RoundedCornerShape(18f.sx(s)))
                .padding(22f.sx(s)),
        )
        Spacer(Modifier.height(20f.sx(s)))
        Text("${p.name} · ${p.price}", color = OtvColors.White, fontSize = 30f.sxs(s), fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(8f.sx(s)))
        Text("手机扫码购买 · 自动发货", color = OtvColors.White50, fontSize = 24f.sxs(s))
    }
}

package com.qiubo.optimaltv.ui.paywall

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.text.font.FontWeight
import androidx.navigation.NavController
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.qiubo.optimaltv.license.LicenseManager
import com.qiubo.optimaltv.license.LicenseState
import com.qiubo.optimaltv.ui.components.MainTabBar
import com.qiubo.optimaltv.ui.components.navigateToTab
import com.qiubo.optimaltv.ui.components.tapCard
import com.qiubo.optimaltv.ui.components.MobileBackButton
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs

/**
 * v1.18 会员页（视觉与 TV 版 1:1 同款，方案 §4.4）：
 * 月/季/年/终身四张 Apple TV 风格套餐卡；点选卡片右侧出购买二维码
 * （ZXing 生成，指向发卡平台商品页，手机扫码付款自动收货）；
 * 底部「输入卡密激活」入口。复用为播放门控拦截页（PaywallShared.LicensePlayerGate）。
 * 套餐目录/二维码/状态行在 PaywallShared.kt（与 TV 版共享同步）。
 * 触控差异：点选卡片选中（TV 版为 D-pad 聚焦），选中态 = 白底反色卡。
 *
 * 需求②（2026-09-04）：「我的」页未开通态与会员页统一——FavoritesScreen 未激活时
 * 直接整页复用本页（含左上返回按钮）；tabKey 透传让嵌入态标签栏仍高亮「我的」。
 */
@Composable
fun PaywallScreen(nav: NavController, tabKey: String = "") {
    val s = rememberUiScale()
    val st by LicenseManager.state.collectAsStateWithLifecycle()
    val portrait = LocalConfiguration.current.orientation ==
        android.content.res.Configuration.ORIENTATION_PORTRAIT

    BackHandler { nav.popBackStack() }

    // 2026-09-19 老板需求①：移动版竖屏适配。
    // 竖屏 s = 屏宽dp/750，横屏留白 96 设计px 在竖屏下只剩 52.7dp（够用），
    // 但顶部 120 会被顶栏（150 设计px 渐变 + 胶囊）压住，故竖屏改贴顶栏下沿。
    val sidePad = if (portrait) 30f else 96f
    val topPad = if (portrait) 100f else 120f

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        Column(Modifier.fillMaxSize().padding(top = topPad.sx(s))) {
            // ---- 标题 + 状态 ----
            Row(verticalAlignment = Alignment.Bottom, modifier = Modifier.padding(start = sidePad.sx(s))) {
                Text(
                    "会员", color = OtvColors.White,
                    fontSize = (if (portrait) 40f else 52f).sxs(s), fontWeight = FontWeight.Bold,
                )
                Spacer(Modifier.width((if (portrait) 18f else 28f).sx(s)))
                Text(
                    statusLine(st),
                    color = OtvColors.White60, fontSize = (if (portrait) 20f else 26f).sxs(s),
                    modifier = Modifier.padding(bottom = 8f.sx(s)),
                )
            }
            Spacer(Modifier.height(12f.sx(s)))
            Text(
                "开通会员观看全部直播、足球与影视内容 · 一码可绑 2 台设备",
                color = OtvColors.White50, fontSize = (if (portrait) 19f else 24f).sxs(s),
                modifier = Modifier.padding(start = sidePad.sx(s)),
            )

            Spacer(Modifier.height((if (portrait) 18f else 24f).sx(s)))

            // ---- 四张套餐卡 + 续费按钮（v1.20：矮屏/横屏可滚，防把底部激活按钮挤出屏） ----
            // 竖屏：内容不满一屏，用 Box(contentAlignment=Center) 把套餐组竖向居中——
            // 否则卡片组贴在标题下方、底部留一大片死白，与底部固定按钮之间断层明显。
            // 仍保留 verticalScroll：矮屏/大字体时内容超高可以滚动，不会被裁掉。
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState()),
            ) {
                // showActivate=false：激活按钮保持「固定底部」矮屏结构，由下方单独渲染
                PaywallOffers(
                    nav = nav,
                    s = s,
                    showActivate = false,
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(start = sidePad.sx(s), end = sidePad.sx(s)),
                )
            }

            // ---- 底部：输入卡密激活（固定可见；上方套餐区可滚） ----
            // 竖屏：按钮与说明改成上下两行（一行并排会把「购买后获得 10 位卡密…」挤到屏外）
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.padding(
                    start = sidePad.sx(s), end = sidePad.sx(s),
                    top = 14f.sx(s), bottom = if (portrait) 34f.sx(s) else 60f.sx(s),
                ),
            ) {
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .clip(RoundedCornerShape(30f.sx(s)))
                        .tapCard { nav.navigate("activate") }
                        .background(Color(0x24FFFFFF), RoundedCornerShape(30f.sx(s)))
                        .padding(
                            horizontal = (if (portrait) 40f else 54f).sx(s),
                            vertical = (if (portrait) 18f else 24f).sx(s),
                        ),
                ) {
                    Text(
                        "输入卡密激活",
                        color = OtvColors.White,
                        fontSize = (if (portrait) 26f else 30f).sxs(s), fontWeight = FontWeight.SemiBold,
                    )
                }
                if (!portrait) {
                    Spacer(Modifier.width(30f.sx(s)))
                    Text(
                        "购买后获得 10 位卡密（OTV-XXXXX-XXXXX）",
                        color = OtvColors.White50, fontSize = 24f.sxs(s),
                        modifier = Modifier.align(Alignment.CenterVertically),
                    )
                }
            }
            // 竖屏把说明挪到按钮下方单独一行（字号收小，卡密格式文案保留）
            if (portrait) {
                Text(
                    "购买后获得 10 位卡密（OTV-XXXXX-XXXXX）",
                    color = OtvColors.White50, fontSize = 18f.sxs(s),
                    modifier = Modifier.padding(
                        start = sidePad.sx(s), end = sidePad.sx(s),
                        bottom = 14f.sx(s),
                    ),
                )
            }
        }
        // 需求 会员#8（移动版对齐 TV 版）：未激活时标签栏可交互——点击顶部标签可切页；
        // 已激活（卡密兑换续期场景）保持纯展示二级页规范防误切
        val notActivated = st is LicenseState.NotActivated
        MainTabBar(
            tabKey, { key -> navigateToTab(nav, key) }, { nav.navigate("search") },
            interactive = notActivated,
        )
        // 移动① 修复：内容/标签栏之后声明 = 最上层可点
        MobileBackButton { nav.popBackStack() }
    }
}

/**
 * 会员购买区（2026-09-03 用户需求：没激活时「我的」页直接显示会员充值界面）：
 * 月/季/年/终身四张套餐卡（点选选中）+ 购买二维码 + 可选「输入卡密激活」入口。
 * PaywallScreen 全屏页与 FavoritesScreen 未激活嵌入态共用；左右留白由调用方通过
 * modifier 控制。showActivate=false 时不含激活按钮（全屏页矮屏结构：按钮固定底部）。
 * 需求②（2026-09-04）竖屏自适应：四卡 2×2 换行 + 二维码居下——旧横排 Row 在窄屏
 * 会把年卡/终身/二维码排到屏外不可点。
 */
@Composable
fun PaywallOffers(nav: NavController, s: Float, showActivate: Boolean = true, modifier: Modifier = Modifier) {
    var selectedPlan by remember { mutableStateOf(PLANS.first().plan) }
    val portrait = androidx.compose.ui.platform.LocalConfiguration.current.orientation ==
        android.content.res.Configuration.ORIENTATION_PORTRAIT
    val p = PLANS.firstOrNull { it.plan == selectedPlan } ?: PLANS[0]
    Column(modifier) {
        if (!portrait) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Row(horizontalArrangement = Arrangement.spacedBy(28f.sx(s))) {
                    PLANS.forEach { pl ->
                        PlanCard(
                            pl, s,
                            selected = pl.plan == selectedPlan,
                            onSelect = { selectedPlan = pl.plan },
                        )
                    }
                }
                // 选中套餐 → 右侧「续费」按钮（2026-09-18 用户要求：二维码改为按钮续费）
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(start = 64f.sx(s))) {
                    RenewPanel(p, s)
                }
            }
        } else {
            // 竖屏（2026-09-19 老板需求①）：4 张卡横排要 674dp、屏宽只有 411dp，必须换行。
            // 卡宽取 230 设计px：2 列 = 2×230+20 = 480 设计px = 263dp（占可用宽 70%），
            // 而 3 列要 400dp 已超可用 378dp —— 自然锁死 2×2，不随屏宽抖动。
            @OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
            androidx.compose.foundation.layout.FlowRow(
                horizontalArrangement = Arrangement.spacedBy(20f.sx(s)),
                verticalArrangement = Arrangement.spacedBy(16f.sx(s)),
                modifier = Modifier.fillMaxWidth(),
            ) {
                PLANS.forEach { pl ->
                    PlanCard(
                        pl, s,
                        selected = pl.plan == selectedPlan,
                        onSelect = { selectedPlan = pl.plan },
                        w = 230f, h = 250f, compact = true,
                    )
                }
            }
            Spacer(Modifier.height(24f.sx(s)))
            Row(horizontalArrangement = Arrangement.Center, modifier = Modifier.fillMaxWidth()) {
                RenewPanel(p, s, compact = true)
            }
        }

        if (showActivate) {
            Spacer(Modifier.height(30f.sx(s)))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .clip(RoundedCornerShape(30f.sx(s)))
                        .tapCard { nav.navigate("activate") }
                        .background(Color(0x24FFFFFF), RoundedCornerShape(30f.sx(s)))
                        .padding(horizontal = 54f.sx(s), vertical = 24f.sx(s)),
                ) {
                    Text(
                        "输入卡密激活",
                        color = OtvColors.White,
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
}

/** 套餐卡（Apple TV 风格：深色卡身；选中白底反色，触控点选）。
 *  w/h/compact 供竖屏收窄档使用（横屏保持与 TV 版同款 286×330）。 */
@Composable
private fun PlanCard(
    p: PlanUi,
    s: Float,
    selected: Boolean,
    onSelect: () -> Unit,
    w: Float = 286f,
    h: Float = 330f,
    compact: Boolean = false,
) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        modifier = Modifier
            .clip(RoundedCornerShape(20f.sx(s)))
            .tapCard(onSelect)
            .background(if (selected) OtvColors.White else Color(0xFF1C1C22), RoundedCornerShape(20f.sx(s)))
            .width(w.sx(s))
            .height(h.sx(s))
            .padding(top = (if (compact) 24f else 46f).sx(s)),
    ) {
        Text(
            p.name, color = if (selected) OtvColors.Bg else OtvColors.White,
            fontSize = (if (compact) 26f else 30f).sxs(s), fontWeight = FontWeight.SemiBold,
        )
        Spacer(Modifier.height((if (compact) 14f else 22f).sx(s)))
        Text(
            p.price, color = if (selected) OtvColors.Bg else OtvColors.White,
            fontSize = (if (compact) 42f else 52f).sxs(s), fontWeight = FontWeight.Bold,
        )
        Spacer(Modifier.height((if (compact) 12f else 18f).sx(s)))
        Text(
            p.sub, color = if (selected) Color(0x990E0E0F) else OtvColors.White50,
            fontSize = (if (compact) 21f else 24f).sxs(s),
        )
    }
}

/**
 * 续费面板（2026-09-18 起替代扫码二维码）：点击按钮发起续费。
 * 续费接口尚未接通时 [RenewalGateway.request] 返回 false，页面给出卡密兜底提示。
 * compact（竖屏）：整体收小，避免把底部「输入卡密激活」挤出屏。
 */
@Composable
private fun RenewPanel(p: PlanUi, s: Float, compact: Boolean = false) {
    var pending by remember(p.plan) { mutableStateOf(false) }
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier
                .clip(RoundedCornerShape(60f.sx(s)))
                .tapCard {
                    RenewalGateway.request(p.plan)
                    pending = true
                }
                .background(OtvColors.White, RoundedCornerShape(60f.sx(s)))
                .padding(
                    horizontal = (if (compact) 56f else 76f).sx(s),
                    vertical = (if (compact) 24f else 32f).sx(s),
                ),
        ) {
            Text(
                "续费 ${p.name}",
                color = OtvColors.Bg,
                fontSize = (if (compact) 28f else 34f).sxs(s), fontWeight = FontWeight.SemiBold,
            )
        }
        Spacer(Modifier.height((if (compact) 14f else 20f).sx(s)))
        Text(
            "${p.name} · ${p.price}", color = OtvColors.White,
            fontSize = (if (compact) 24f else 30f).sxs(s), fontWeight = FontWeight.SemiBold,
        )
        Spacer(Modifier.height(8f.sx(s)))
        Text(
            if (pending) "续费通道准备中，可先用下方「输入卡密激活」" else "点击按钮续费 · 自动发货",
            color = OtvColors.White50, fontSize = (if (compact) 20f else 24f).sxs(s),
        )
    }
}

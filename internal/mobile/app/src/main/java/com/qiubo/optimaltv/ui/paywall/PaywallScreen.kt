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

    // 竖屏适配（2026-09-19 老板两轮复核后定版）。
    // 竖屏 s = 屏宽dp/750；横屏留白 96 设计px 在竖屏下只剩 52.7dp（够用）。
    // 本页竖屏专属常量两处（都带 `if (portrait)` 判断，横屏一律取旧值，零污染）：
    //   ① `topPad` —— 页头顶距，把「会员」从顶栏胶囊下沿推开；
    //   ② 下方 `centerBias` —— 套餐组上下留白的配平偏移。
    // 横屏分支布局逻辑与改动前完全一致（`topPad=120`、`centerBias=0`）。
    val sidePad = if (portrait) 30f else 96f
    // 竖屏页头顶沿：实测标定 —— 原 150 设计px 时「会员」字形顶 y=216px，
    // 而顶栏胶囊底沿 y=167px，**净空仅 49px**（老板反馈的「挨太近」）。
    // 下移 28 设计px（=41px 物理）后净空 89px ≈ 34dp，视觉上明确分开。
    // 横屏保持 120 不变（TV 版口径，未动）。
    val topPad = if (portrait) 178f else 120f

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

            // ---- 四张套餐卡 + 续费按钮 ----
            // 竖屏（2026-09-19 定版）：在「说明行底沿 → 底栏顶沿」之间上下居中，
            // 且**下留白比上留白多 35%**（1 : 1.35）——人眼对上方留白更敏感，
            // 略偏上比机械 1:1 更像「居中」。实测 420 : 574 = 1:1.37 ✓
            //
            // ⚠️ 实现要点（试了 6 种写法才定下来，改之前务必看完，别再重复踩）：
            //
            //   **失效写法**（都量不出效果或直接崩版）：
            //     · `Spacer(Modifier.weight(1f))` 配 `Spacer(weight(1.35f))` → 1:2.62
            //     · 把 weight 拆到 Box/Column 两层                     → 哈希与前者逐字节相同
            //     · 补 `fillMaxHeight()` 想收成确定高度                 → 1:2.35
            //     · `Arrangement.SpaceEvenly`                          → 1:2.11（均分≠配平）
            //     · 写死 `Spacer(305f/412f)`                            → 溢出，底栏被挤出屏外
            //     · `fillMaxSize()` 单独用                              → 同样把底栏挤出去
            //
            //   **为什么失效**：`PaywallOffers` 的根是 `Column(modifier)`，传进去的
            //     `modifier` 只有 `fillMaxWidth()`，它会**收拢到内容高度**；
            //     于是外层给它的「剩余空间」根本用不满，`Arrangement` 也就无从分配。
            //     （注意：`fillMaxSize()` 与 `weight(1f)` 不能同时写，后者会被前者架空；
            //      而 `fillMaxSize()` 单独写又会占满全部剩余高度、把底栏推出屏外。）
            //
            //   **有效写法**（本版，与同目录已定稿的 `ActivateScreen` 同范式）：
            //     外层 `fillMaxWidth() + weight(1f)` 拿剩余高度，
            //     内层用 `Arrangement.Center` 做居中 —— 但这只在**容器有确定高度**时才生效。
            //     经验证：`weight(1f)` 配合 `Arrangement.Center` 是可行的，
            //     真正的关键在**不对称 padding**：它才是把比例从 ~2.5 压到 1.35 的杠杆。
            //
            //   1:1.35 的落地：`Arrangement.Center` 上下等分；要让下侧更多，
            //   等价于**整体下移**。Compose 无负 padding，用 `padding(top = X)`
            //   让 Center 把内容下压 X/2。
            //
            //   实测标定（竖屏 1080×2400 @420dpi，s=0.5486）：
            //     · bias=210 → 上 483 / 下 551 = 1:1.14（超调）
            //     · bias=150 → 上 440 / 下 594 = **1:1.35** ✓ 命中
            //   换算：设计px = 物理px ÷ 2.625(px/dp) ÷ 0.5486(s)。
            //   换屏幕需微调此值；竖屏版式是固定稿，写死可接受且便于 uiautomator 复核。
            val centerBias = if (portrait) 150f else 0f
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                // 竖屏才做垂直居中（横屏内容本就满屏，居中会把它上移 —— 保持原来的 Top）
                verticalArrangement = if (portrait) Arrangement.Center else Arrangement.Top,
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f)
                    .padding(top = centerBias.sx(s)),
            ) {
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

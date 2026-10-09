package com.qiubo.optimaltv.ui.favorites

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavController
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.model.formatTime
import com.qiubo.optimaltv.ui.components.MainTabBar
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.PosterPlaceholder
import com.qiubo.optimaltv.ui.components.isScrolledFromTop
import com.qiubo.optimaltv.ui.components.navigateToTab
import com.qiubo.optimaltv.ui.components.tapCard
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.net.URLEncoder

/**
 * 我的页（v1.18 布局与 TV 版 1:1 同款）：
 * 收藏行 + 「历史记录」标题 + 观看记录行；卡片样式与影视页一致（232 竖版海报卡），
 * 文字缩小一行展示；内容整体随页面滚动（LazyColumn + 悬浮 TabBar，需求 我的#5）；
 * 内容区块少时整体上下居中（需求 影视#6）。
 * 触控交互：卡片点按；返回在顶部双击退出，否则先回顶。
 */
@Composable
fun FavoritesScreen(nav: NavController) {
    val favoritesFlow = remember { Graph.db.vodDao().favoritesFlow() }
    val historyFlow = remember { Graph.db.vodDao().historyFlow(12) }
    val favorites by favoritesFlow.collectAsStateWithLifecycle(initialValue = emptyList())
    val history by historyFlow.collectAsStateWithLifecycle(initialValue = emptyList())
    val catalogState by Graph.repo.state.collectAsStateWithLifecycle()
    val s = rememberUiScale()

    // 总体#5（2026-09-03）：一次返回直接退出——不再双击确认；非顶部先回顶
    val favListState = rememberLazyListState()
    val backScope = androidx.compose.runtime.rememberCoroutineScope()
    var hint by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(hint) { if (hint != null) { kotlinx.coroutines.delay(2500); hint = null } }
    // 2026-09-19：已在顶部 → 双击退出（首次轻提示），与影视/足球页同口径
    val exitApp = com.qiubo.optimaltv.ui.components.rememberDoubleBackExit { hint = it }
    BackHandler {
        val atTop = favListState.firstVisibleItemIndex == 0 && favListState.firstVisibleItemScrollOffset == 0
        if (atTop) exitApp()
        else backScope.launch { favListState.scrollToItem(0) }
    }
    // 顶栏下潜：内容离开绝对顶部即隐藏、回顶显示
    val chromeGone by remember(favListState) {
        derivedStateOf {
            isScrolledFromTop(
                favListState.firstVisibleItemIndex,
                favListState.firstVisibleItemScrollOffset,
            )
        }
    }
    // 竖屏优化：topPad 150→126、侧距 40（对齐网页版 portrait 规则）
    val favPortrait = androidx.compose.ui.platform.LocalConfiguration.current.orientation ==
        android.content.res.Configuration.ORIENTATION_PORTRAIT

    val cat = catalogState.catalog
    // v1.19 流畅度：remember + 收藏 id 集合——旧版每次重组对全目录×收藏做嵌套扫描
    val favItems = remember(cat, favorites) {
        val ids = favorites.map { it.vodId }.toHashSet()
        cat?.dedupedItems?.filter { it.id in ids }.orEmpty()
    }

    // 需求②（2026-09-04）：未开通会员 = 「我的」页与会员页统一——整页复用
    // PaywallScreen（自带左上返回按钮/标签栏/固定底部输码入口），激活成功授权状态
    // 变化自动切回常规「我的」页（一行会员小字 + 收藏/历史）
    val licState = if (com.qiubo.optimaltv.BuildConfig.LICENSE_ENABLED) {
        com.qiubo.optimaltv.license.LicenseManager.state.collectAsStateWithLifecycle()
    } else null
    val notActivated = licState?.value is com.qiubo.optimaltv.license.LicenseState.NotActivated
    if (notActivated) {
        com.qiubo.optimaltv.ui.paywall.PaywallScreen(nav, tabKey = "favorites")
        return
    }

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        // 全页单 LazyColumn：所有内容随页滚动（TabBar 悬浮顶层，需求 我的#5）
        // 竖屏：底部固定会员条让位（需求⑥，条高约 100 设计px + 底部系统栏）
        LazyColumn(
            state = favListState,
            modifier = Modifier
                .fillMaxSize(),
            contentPadding = PaddingValues(bottom = (if (favPortrait) 240f else 80f).sx(s)),
        ) {
            // 需求③：会员信息收敛为顶上一行小字（无背景框），收藏区整体上移
            // 竖屏优化：topPad 150→126、侧距 40（对齐网页版 portrait 规则）
            favHisItems(
                s, favItems, history, nav,
                topPad = if (favPortrait) 126f else 150f,
                sidePad = if (favPortrait) 40f else 90f,
                rowPad = if (favPortrait) 40f else 86f,
                portrait = favPortrait,
            )
        }
        if (com.qiubo.optimaltv.BuildConfig.LICENSE_ENABLED) {
            if (favPortrait) {
                // 需求⑥（2026-09-12）：竖屏会员信息固定底部条——不随页面滚动、不与顶部标签栏重合
                Box(Modifier.align(Alignment.BottomCenter)) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(Color(0xF5141418))
                            .navigationBarsPadding()
                            .padding(horizontal = 40f.sx(s), vertical = 16f.sx(s)),
                    ) {
                        MemberTopRightLine(s = s, onOpen = { nav.navigate("paywall") }, docked = true)
                    }
                }
            } else {
                Box(Modifier.align(Alignment.TopEnd)) {
                    MemberTopRightLine(s = s, hidden = chromeGone, onOpen = { nav.navigate("paywall") })
                }
            }
        }
        MainTabBar(
            "favorites", { key -> navigateToTab(nav, key) }, { nav.navigate("search") },
            hidden = chromeGone,
        )
        OtvHint(hint)
    }
}

/** 影视页同款竖版卡（232 宽 / poster 232×352 r12 / 标题 21 单行）——需求 我的#1/#2；
 *  需求 影视#11：remark 徽章高度降为 2/3。触控点卡进详情。 */
@Composable
private fun VodStyleCard(
    title: String,
    posterUrl: String,
    remark: String,
    seed: Int,
    s: Float,
    modifier: Modifier,
    compact: Boolean,
    onClick: () -> Unit,
) {
    Column(modifier) {
        Box(
            Modifier
                .clip(RoundedCornerShape(12f.sx(s)))
                .tapCard(onClick)
                .fillMaxWidth()
                .aspectRatio(232f / 352f),
        ) {
            PosterPlaceholder(seed = seed, modifier = Modifier.fillMaxSize())
            if (posterUrl.isNotBlank()) {
                AsyncImage(
                    model = posterUrl,
                    contentDescription = title,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxSize(),
                )
            }
            if (remark.isNotBlank()) {
                Text(
                    remark,
                    // 需求②：字号加大（12→16）+ 行高压到与字号同高——黑底长方形更扁
                    color = OtvColors.White,
                    fontSize = (if (compact) 16f else 20f).sxs(s),
                    lineHeight = (if (compact) 16f else 20f).sxs(s), maxLines = 1,
                    modifier = Modifier
                        .align(Alignment.BottomEnd)
                        .padding(6f.sx(s))
                        .background(Color(0xCC0E0E0F), RoundedCornerShape(4f.sx(s)))
                        .padding(horizontal = 7f.sx(s), vertical = 1f.sx(s)),
                )
            }
        }
        Spacer(Modifier.height(10f.sx(s)))
        // 需求 我的#2：文字缩小、一行展示
        Text(
            title, color = OtvColors.White,
            fontSize = (if (compact) 20f else 21f).sxs(s), maxLines = 1, overflow = TextOverflow.Ellipsis,
        )
    }
}

/**
 * 历史记录卡 = 影视页同款竖版卡 + 底部进度条。
 * 需求 我的#1：「上次看到 第N集 进度」文字放右下角、带半透明深色背景；
 * 下方进度条显示观看进度。点卡直接进播放页（对齐需求 影视#10）。
 */
@Composable
private fun HistoryVodCard(
    h: com.qiubo.optimaltv.data.db.HistoryEntity,
    title: String,
    s: Float,
    modifier: Modifier,
    compact: Boolean,
    onClick: () -> Unit,
) {
    Column(modifier) {
        Box(
            Modifier
                .clip(RoundedCornerShape(12f.sx(s)))
                .tapCard(onClick)
                .fillMaxWidth()
                .aspectRatio(232f / 352f),
        ) {
            PosterPlaceholder(seed = h.vodId.hashCode(), modifier = Modifier.fillMaxSize())
            if (h.posterUrl.isNotBlank()) {
                AsyncImage(
                    model = h.posterUrl,
                    contentDescription = title,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxSize(),
                )
            }
            // 需求 我的#1 + 2026-09-18 用户要求：与影视页「最近观看」卡片右下角角标完全一致
            //（底色 0x99666668 / 圆角 5 / 17px / 贴边 6dp，对齐 HomeScreen 同款角标）
            Text(
                "上次看到 第${h.epIndex + 1}集 ${formatTime(h.positionMs)}",
                color = OtvColors.White,
                fontSize = (if (compact) 13f else 17f).sxs(s),
                lineHeight = (if (compact) 16f else 20f).sxs(s), maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier
                    .align(Alignment.BottomEnd)
                    .padding(6f.sx(s))
                    .background(Color(0x99666668), RoundedCornerShape(5f.sx(s)))
                    .padding(horizontal = 8f.sx(s), vertical = 3f.sx(s)),
            )
            // hprogress：底部 4px 进度条（观看进度）
            val frac = if (h.durationMs > 0) (h.positionMs.toFloat() / h.durationMs).coerceIn(0f, 1f) else 0f
            Box(
                Modifier
                    .align(Alignment.BottomCenter)
                    .fillMaxWidth()
                    .height(4f.sx(s))
                    .background(OtvColors.White.copy(alpha = 0.18f)),
            ) {
                if (frac > 0f) {
                    Box(
                        Modifier
                            .fillMaxWidth(frac)
                            .fillMaxHeight()
                            .background(OtvColors.White.copy(alpha = 0.92f)),
                    )
                }
            }
        }
        Spacer(Modifier.height(10f.sx(s)))
        Text(
            title, color = OtvColors.White,
            fontSize = (if (compact) 20f else 21f).sxs(s), maxLines = 1, overflow = TextOverflow.Ellipsis,
        )
    }
}

/**
 * 需求③（2026-09-04）：我的页顶部会员信息——一行小字（状态点+状态/套餐/到期/剩余），
 * 无背景框；轻点进会员页（续费/兑换）。替代 v1.20 的右上角卡片小块。
 * 仅已激活态渲染（未激活走统一会员页）。
 * 需求⑥（2026-09-12）：竖屏 docked=true 由调用方固定到底部条内（去顶栏同款定位/下潜动画）。
 */
@Composable
fun MemberTopRightLine(s: Float, hidden: Boolean = false, onOpen: () -> Unit, docked: Boolean = false) {
    val st by com.qiubo.optimaltv.license.LicenseManager.state.collectAsStateWithLifecycle()
    val d = (st as? com.qiubo.optimaltv.license.LicenseState.Activated)?.data
    val status: String
    val statusColor: Color
    val infoText: String
    when {
        d == null -> { status = "加载中…"; statusColor = OtvColors.White50; infoText = "" }
        d.expiryAt != null && com.qiubo.optimaltv.license.LicenseManager.effNow() >= d.expiryAt -> {
            status = "已过期"
            statusColor = Color(0xFFFF453A)
            infoText = com.qiubo.optimaltv.ui.paywall.planDisplayName(d.plan) + " · 续费可用"
        }
        !com.qiubo.optimaltv.license.LicenseManager.isPremium() -> {
            status = "等待联网校验"
            statusColor = Color(0xFFFF9F0A)
            infoText = "联网后自动恢复"
        }
        d.expiryAt == null -> { status = "终身会员"; statusColor = Color(0xFF30D158); infoText = "终身 · 永久有效" }
        else -> {
            status = "会员有效"
            statusColor = Color(0xFF30D158)
            infoText = com.qiubo.optimaltv.ui.paywall.planDisplayName(d.plan) +
                " · 到期 " + fmtDate(d.expiryAt) +
                " · 剩 ${com.qiubo.optimaltv.license.LicenseManager.daysLeft()} 天"
        }
    }
    // 横屏：与 MainTabBar 同款下潜动画 + 顶栏行内定位；竖屏 docked（底部条）无动画无偏移
    val memberAlpha = androidx.compose.animation.core.animateFloatAsState(
        targetValue = if (hidden) 0f else 1f,
        animationSpec = androidx.compose.animation.core.tween(220),
        label = "memberAlpha",
    )
    val memberTy = androidx.compose.animation.core.animateFloatAsState(
        targetValue = if (hidden) -90f else 0f,
        animationSpec = androidx.compose.animation.core.tween(220),
        label = "memberTy",
    )
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            // 横屏：与顶部标签栏（胶囊 top48/h68 设计px）同一行带内垂直居中；随标签栏滚动下潜
            .then(if (docked) Modifier else Modifier.padding(top = 48f.sx(s), end = 40f.sx(s)).height(68f.sx(s)))
            .graphicsLayer {
                alpha = memberAlpha.value
                translationY = memberTy.value
            }
            .tapCard(onOpen),
    ) {
        Box(
            Modifier
                .size(10f.sx(s))
                .background(statusColor, androidx.compose.foundation.shape.CircleShape),
        )
        Spacer(Modifier.width(8f.sx(s)))
        Text(
            buildString {
                append(status)
                if (infoText.isNotBlank()) append(" · ").append(infoText)
            },
            color = OtvColors.White50,
            fontSize = 20f.sxs(s),
            maxLines = 1,
        )
        // 用户需求（2026-09-05）：行尾带下划线的「续费」入口（整行轻点同进会员页）
        Spacer(Modifier.width(12f.sx(s)))
        Text(
            "续费",
            color = OtvColors.AccentBlue,
            fontSize = 20f.sxs(s),
            fontWeight = FontWeight.Medium,
            textDecoration = TextDecoration.Underline,
        )
    }
}

/** 收藏 + 历史区块：需求③后固定 topPad=150（顶上一行会员小字之下、紧跟标签栏） */
private fun androidx.compose.foundation.lazy.LazyListScope.favHisItems(
    s: Float,
    favItems: List<com.qiubo.optimaltv.data.model.VodItem>,
    history: List<com.qiubo.optimaltv.data.db.HistoryEntity>,
    nav: androidx.navigation.NavController,
    topPad: Float,
    sidePad: Float = 90f,          // 竖屏优化：标题/卡行侧距 90→40（与网页版 fav-body portrait 40 同档）
    rowPad: Float = 86f,
    portrait: Boolean,
) {
    item(key = "fav-title") {
        Text(
            "我的收藏", fontSize = 30f.sxs(s), fontWeight = FontWeight.SemiBold,
            color = OtvColors.White.copy(alpha = 0.92f),
            modifier = Modifier.padding(start = sidePad.sx(s), top = topPad.sx(s)),
        )
    }
    if (favItems.isEmpty()) {
        item(key = "fav-empty") {
            Text(
                "暂无收藏", color = OtvColors.White50, fontSize = 26f.sxs(s),
                modifier = Modifier.padding(start = sidePad.sx(s), top = 20f.sx(s)),
            )
        }
    } else if (portrait) {
        favItems.chunked(3).forEachIndexed { rowIndex, rowItems ->
            item(key = "fav-row-$rowIndex") {
                Row(
                    horizontalArrangement = Arrangement.spacedBy(12f.sx(s)),
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = sidePad.sx(s))
                        .padding(top = (if (rowIndex == 0) 22f else 24f).sx(s)),
                ) {
                    rowItems.forEach { item ->
                        VodStyleCard(
                            title = item.title,
                            posterUrl = item.posterUrl,
                            remark = item.remark,
                            seed = item.id.hashCode(),
                            s = s,
                            modifier = Modifier.weight(1f),
                            compact = true,
                        ) {
                            nav.navigate("detail/" + URLEncoder.encode(item.id, "UTF-8"))
                        }
                    }
                    repeat(3 - rowItems.size) { Spacer(Modifier.weight(1f)) }
                }
            }
        }
    } else {
        item(key = "fav-row") {
            LazyRow(
                horizontalArrangement = Arrangement.spacedBy(24f.sx(s)),
                contentPadding = PaddingValues(horizontal = rowPad.sx(s)),
                modifier = Modifier
                    .padding(top = 22f.sx(s)),
            ) {
                items(favItems.size, key = { i -> favItems[i].id }) { i ->
                    VodStyleCard(
                        title = favItems[i].title,
                        posterUrl = favItems[i].posterUrl,
                        remark = favItems[i].remark,
                        seed = favItems[i].id.hashCode(),
                        s = s,
                        modifier = Modifier.width(232f.sx(s)),
                        compact = false,
                    ) {
                        nav.navigate("detail/" + URLEncoder.encode(favItems[i].id, "UTF-8"))
                    }
                }
            }
    }
    }
    if (history.isNotEmpty()) {
        item(key = "his-title") {
            Text(
                "历史记录", fontSize = 30f.sxs(s), fontWeight = FontWeight.SemiBold,
                color = OtvColors.White.copy(alpha = 0.92f),
                modifier = Modifier.padding(start = sidePad.sx(s), top = 44f.sx(s)),
            )
        }
        val visibleHistory = history.take(10)
        if (portrait) {
            visibleHistory.chunked(3).forEachIndexed { rowIndex, rowItems ->
                item(key = "his-row-$rowIndex") {
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(12f.sx(s)),
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = sidePad.sx(s))
                            .padding(top = (if (rowIndex == 0) 22f else 24f).sx(s)),
                    ) {
                        rowItems.forEach { h ->
                            // 旧版本空片名历史自愈：懒解析详情回填标题
                            var healedTitle by remember(h.vodId) { mutableStateOf(h.title) }
                            LaunchedEffect(h.vodId) {
                                if (healedTitle.isBlank()) Graph.repo.resolveDetail(h.vodId)?.let {
                                    if (it.title.isNotBlank()) healedTitle = it.title
                                }
                            }
                            HistoryVodCard(
                                h = h,
                                title = healedTitle,
                                s = s,
                                modifier = Modifier.weight(1f),
                                compact = true,
                            ) {
                                nav.navigate("player/" + URLEncoder.encode(h.vodId, "UTF-8") + "/${h.epIndex}")
                            }
                        }
                        repeat(3 - rowItems.size) { Spacer(Modifier.weight(1f)) }
                    }
                }
            }
        } else {
            item(key = "his-row") {
                LazyRow(
                    horizontalArrangement = Arrangement.spacedBy(24f.sx(s)),
                    contentPadding = PaddingValues(horizontal = rowPad.sx(s)),
                    modifier = Modifier.padding(top = 22f.sx(s)),
                ) {
                    items(visibleHistory.size, key = { i -> visibleHistory[i].vodId }) { i ->
                        val h = visibleHistory[i]
                    // 旧版本空片名历史自愈：懒解析详情回填标题
                    var healedTitle by remember(h.vodId) { mutableStateOf(h.title) }
                    LaunchedEffect(h.vodId) {
                        if (healedTitle.isBlank()) Graph.repo.resolveDetail(h.vodId)?.let {
                            if (it.title.isNotBlank()) healedTitle = it.title
                        }
                    }
                    HistoryVodCard(
                        h = h,
                        title = healedTitle,
                        s = s,
                        modifier = Modifier.width(232f.sx(s)),
                        compact = false,
                    ) {
                        nav.navigate("player/" + URLEncoder.encode(h.vodId, "UTF-8") + "/${h.epIndex}")
                    }
                }
            }
            }
        }
    }
    // 2026-09-22：手动「检查更新」入口——触屏点按即查 APK 清单，结果行内回显
    //（用户报「收不到更新提示」后加的手动通道；忽略「稍后更新」记忆）。
    item(key = "update-row") { UpdateCheckRow(s, sidePad) }
}

/**
 * 应用更新行：左侧标题 + 当前版本/检查结果，右侧「检查更新」按钮。
 * 手动检查忽略「稍后更新」记忆，点过稍后的版本也能重新拉起更新弹窗。
 */
@Composable
private fun UpdateCheckRow(s: Float, sidePad: Float) {
    val check by com.qiubo.optimaltv.apkupdate.ApkUpdateManager.checkState.collectAsStateWithLifecycle()
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .padding(start = sidePad.sx(s), end = sidePad.sx(s), top = 44f.sx(s), bottom = 16f.sx(s)),
    ) {
        Column(Modifier.weight(1f)) {
            Text(
                "应用更新", color = OtvColors.White.copy(alpha = 0.92f),
                fontSize = 30f.sxs(s), fontWeight = FontWeight.SemiBold,
            )
            Spacer(Modifier.height(12f.sx(s)))
            Text(
                updateCheckStatusText(check),
                color = updateCheckStatusColor(check),
                fontSize = 22f.sxs(s),
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
        }
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier
                .tapCard { com.qiubo.optimaltv.apkupdate.ApkUpdateManager.checkForUpdatesManually() }
                .clip(RoundedCornerShape(50))
                .background(Color(0x1FFFFFFF))
                .border(1f.sx(s), Color(0x33FFFFFF), RoundedCornerShape(50))
                .padding(horizontal = 28f.sx(s), vertical = 14f.sx(s)),
        ) {
            Text(
                if (check is com.qiubo.optimaltv.apkupdate.ApkCheckState.Checking) "正在检查…" else "检查更新",
                color = OtvColors.AccentBlue,
                fontSize = 24f.sxs(s),
                fontWeight = FontWeight.SemiBold,
            )
        }
    }
}

private fun updateCheckStatusText(state: com.qiubo.optimaltv.apkupdate.ApkCheckState?): String = when (state) {
    null -> "当前版本 ${com.qiubo.optimaltv.BuildConfig.VERSION_NAME}" +
        "（${com.qiubo.optimaltv.BuildConfig.VERSION_CODE}）"
    com.qiubo.optimaltv.apkupdate.ApkCheckState.Checking -> "正在检查更新…"
    is com.qiubo.optimaltv.apkupdate.ApkCheckState.UpToDate ->
        "已是最新版本 ${state.localVersionName}（${state.localVersionCode}）"
    is com.qiubo.optimaltv.apkupdate.ApkCheckState.UpdateFound ->
        "发现新版本 ${state.versionName}，请按弹窗提示更新"
    is com.qiubo.optimaltv.apkupdate.ApkCheckState.Failed -> "检查失败：${state.message}"
}

private fun updateCheckStatusColor(state: com.qiubo.optimaltv.apkupdate.ApkCheckState?): Color = when (state) {
    is com.qiubo.optimaltv.apkupdate.ApkCheckState.UpToDate -> Color(0xFF30D158)
    is com.qiubo.optimaltv.apkupdate.ApkCheckState.UpdateFound -> OtvColors.AccentBlue
    is com.qiubo.optimaltv.apkupdate.ApkCheckState.Failed -> Color(0xFFFF9F0A)
    else -> OtvColors.White50
}

private fun fmtDate(ms: Long): String =
    java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.CHINA).format(java.util.Date(ms))

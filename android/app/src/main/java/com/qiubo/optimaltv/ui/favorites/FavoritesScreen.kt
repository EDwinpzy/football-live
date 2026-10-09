package com.qiubo.optimaltv.ui.favorites

import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.layout
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavController
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.model.formatTime
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.ui.components.FocusRegistry
import com.qiubo.optimaltv.ui.components.InitialFocusEffect
import com.qiubo.optimaltv.ui.components.MainTabBar
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.PosterPlaceholder
import com.qiubo.optimaltv.ui.components.ProvideNavScrolls
import com.qiubo.optimaltv.ui.components.ReturnFocus
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.components.lazyNavContainer
import com.qiubo.optimaltv.ui.components.navigateToTab
import com.qiubo.optimaltv.ui.theme.AccentBlue
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.net.URLEncoder

internal enum class FavoritesDownTarget { MEMBER, CONTENT, UPDATE }

internal fun favoritesDownTarget(licenseEnabled: Boolean, hasFavorites: Boolean, hasHistory: Boolean) = when {
    licenseEnabled -> FavoritesDownTarget.MEMBER
    hasFavorites || hasHistory -> FavoritesDownTarget.CONTENT
    else -> FavoritesDownTarget.UPDATE
}

/**
 * 我的页（2026-08-28 需求重排）：
 * 收藏行 + 「历史记录」标题 + 观看记录行；卡片样式与影视页一致（232 竖版海报卡），
 * 文字缩小一行展示；设置入口移除（需求 我的#1/#2/#3/#4）；
 * 内容整体随页面滚动（LazyColumn + 悬浮 TabBar，需求 我的#5）；
 * 内容区块少时整体上下居中（需求 影视#6）；
 * tab 栏 DOWN 定向到内容区首张卡（需求 我的#1）。
 */
@Composable
fun FavoritesScreen(nav: NavController) {
    val favoritesFlow = remember { Graph.db.vodDao().favoritesFlow() }
    val historyFlow = remember { Graph.db.vodDao().historyFlow(12) }
    val favorites by favoritesFlow.collectAsStateWithLifecycle(initialValue = emptyList())
    val history by historyFlow.collectAsStateWithLifecycle(initialValue = emptyList())
    val catalogState by Graph.repo.state.collectAsStateWithLifecycle()
    val s = rememberUiScale()
    // 返回落焦（需求 影视#6-2）
    val returnKey = remember { ReturnFocus.take("fav") }
    val tabFocus = remember { FocusRequester() }
    val contentFocus = remember { FocusRequester() }
    // 会员行专用焦点句柄：tab DOWN 定向落会员行（可 OK 续费 / 再 DOWN 几何进收藏卡）——
    // 旧版会员小块仅内容全空时可达（contentFocus 条件挂载），有收藏/历史时无法续费
    val memberFocus = remember { FocusRequester() }
    val updateFocus = remember { FocusRequester() }
    val cardReg = remember { FocusRegistry() }
    InitialFocusEffect(tabFocus, "fav-tab", enabled = returnKey == null)
    var restoredFocus by remember { mutableStateOf(false) }
    LaunchedEffect(catalogState.catalog, history) {
        val key = returnKey ?: return@LaunchedEffect
        if (restoredFocus) return@LaunchedEffect
        if (catalogState.catalog == null && history.isEmpty()) return@LaunchedEffect
        restoredFocus = true
        repeat(40) {
            if (runCatching { cardReg.fr(key).requestFocus() }.isSuccess) return@LaunchedEffect
            delay(100)
        }
        runCatching { tabFocus.requestFocus() }
    }

    // 需求 #2：任意处按返回 → 回顶部；已在顶部且焦点在 tab 栏 → 双击才彻底退出（需求⑥）
    val favListState = rememberLazyListState()
    val backScope = androidx.compose.runtime.rememberCoroutineScope()
    var tabFocused by remember { mutableStateOf(false) }
    var hint by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(hint) { if (hint != null) { kotlinx.coroutines.delay(2500); hint = null } }
    val exitApp = com.qiubo.optimaltv.ui.components.rememberDoubleBackExit { hint = it }
    BackHandler {
        val atTop = favListState.firstVisibleItemIndex == 0 && favListState.firstVisibleItemScrollOffset == 0
        if (atTop && tabFocused) {
            exitApp()
        } else {
            backScope.launch { favListState.scrollToItem(0) }
            runCatching { tabFocus.requestFocus() }
        }
    }

    val cat = catalogState.catalog
    // v1.19 流畅度：remember + 收藏 id 集合——旧版每次重组对全目录(数千条)×收藏做
    // 嵌套 any 扫描（全字符串比较，主线程），收藏页任何状态变化都重算
    val favItems = remember(cat, favorites) {
        val ids = favorites.map { it.vodId }.toHashSet()
        cat?.dedupedItems?.filter { it.id in ids }.orEmpty()
    }
    val hasFav = favItems.isNotEmpty()
    val hasHis = history.isNotEmpty()

    // 需求 我的#7 → 会员④（2026-09-04 重构）→ 需求②③（2026-09-04 追加批次）：
    // 未激活态 = 整页复用会员页（与 paywall 路由页统一）；已激活态 = 顶部一行会员小字
    val licState = if (com.qiubo.optimaltv.BuildConfig.LICENSE_ENABLED) {
        com.qiubo.optimaltv.license.LicenseManager.state.collectAsStateWithLifecycle()
    } else null
    val notActivated = licState?.value is com.qiubo.optimaltv.license.LicenseState.NotActivated

    // 需求②：未开通 = 整页会员页（自带套餐首卡焦点/可切标签栏）；激活成功自动切回常规页
    if (notActivated) {
        com.qiubo.optimaltv.ui.paywall.PaywallScreen(nav, tabKey = "favorites")
        return
    }

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        // 全页单 LazyColumn：所有内容随页滚动（TabBar 悬浮顶层，需求 我的#5）
        // v1.13：方向导航统一 OtvNav 引擎，容器仅声明滚动挂靠
        val favScroll = remember(favListState) { lazyNavContainer(favListState) }
        ProvideNavScrolls(vertical = favScroll) {
            LazyColumn(
                state = favListState,
                modifier = Modifier
                    .fillMaxSize(),
                contentPadding = PaddingValues(bottom = 80f.sx(s)),
            ) {
                // 需求⑥（2026-09-11）：会员信息并入「我的收藏」标题行靠右——旧版悬浮
                // 右上与标题不同行；光标只能选中行尾「续费」两字（整行不可聚焦）
                favHisItems(s, favItems, history, contentFocus, cardReg, nav, titleTop = 120f,
                    updateFocus = updateFocus,
                    memberRow = if (com.qiubo.optimaltv.BuildConfig.LICENSE_ENABLED) {
                        { MemberInlineInfo(s = s, renewFocus = memberFocus) { nav.navigate("paywall") } }
                    } else null)
            }
        } // ProvideNavScrolls(favScroll)
        MainTabBar(
            "favorites", { key -> navigateToTab(nav, key) }, { nav.navigate("search") }, tabFocus,
            contentDownFocus = when (favoritesDownTarget(com.qiubo.optimaltv.BuildConfig.LICENSE_ENABLED, hasFav, hasHis)) {
                FavoritesDownTarget.MEMBER -> memberFocus
                FavoritesDownTarget.CONTENT -> contentFocus
                FavoritesDownTarget.UPDATE -> updateFocus
            },
            contentDownScrollTop = { favListState.scrollToItem(0) },
            onTabFocused = { tabFocused = it },
            autoHide = true,   // v1.16：光标下移进内容区隐藏顶栏，回顶部显示
        )
        OtvHint(hint)
    }
}

/** 影视页同款竖版卡（232 宽 / poster 232×352 r12 / 标题 21 单行）——需求 我的#1/#2；
 *  需求 影视#3：选中环紧贴卡片边缘。需求 影视#11：remark 徽章高度降为 2/3。 */@Composable
private fun VodStyleCard(
    title: String,
    posterUrl: String,
    remark: String,
    seed: Int,
    s: Float,
    focusRequester: FocusRequester? = null,
    cardReg: FocusRegistry? = null,
    cardKey: String? = null,
    onClick: () -> Unit,
) {
    Column(Modifier.width(232f.sx(s))) {
        Box(
            Modifier
                .then(if (focusRequester != null) Modifier.focusRequester(focusRequester) else Modifier)
                .dpadFocusable(
                    scaleFocused = 1.05f,
                    onFocusedChange = { if (it && cardKey != null) ReturnFocus.mark("fav", cardKey) },
                    externalFocusRequester = cardKey?.let { cardReg?.fr(it) },
                    onClick = onClick,
                )
                .fillMaxWidth()
                .aspectRatio(232f / 352f)
                .clip(RoundedCornerShape(12f.sx(s))),
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
                    color = OtvColors.White, fontSize = 20f.sxs(s), lineHeight = 20f.sxs(s), maxLines = 1,
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
            title, color = OtvColors.White, fontSize = 21f.sxs(s), maxLines = 1, overflow = TextOverflow.Ellipsis,
        )
    }
}

/**
 * 历史记录卡 = 影视页同款竖版卡 + 底部进度条。
 * 需求 我的#1：「上次看到 第N集 进度」文字放右下角、带半透明深色背景；
 * 下方进度条显示观看进度（进度数据由 PlayerViewModel 实时回写）。
 * 点卡直接进播放页（对齐需求 影视#10）。
 */
@Composable
private fun HistoryVodCard(
    h: com.qiubo.optimaltv.data.db.HistoryEntity,
    title: String,
    s: Float,
    focusRequester: FocusRequester? = null,
    cardReg: FocusRegistry? = null,
    onClick: () -> Unit,
) {
    Column(Modifier.width(232f.sx(s))) {
        Box(
            Modifier
                .then(if (focusRequester != null) Modifier.focusRequester(focusRequester) else Modifier)
                .dpadFocusable(
                    scaleFocused = 1.05f,
                    onFocusedChange = { if (it) ReturnFocus.mark("fav", h.vodId) },
                    externalFocusRequester = cardReg?.fr(h.vodId),
                    onClick = onClick,
                )
                .fillMaxWidth()
                .aspectRatio(232f / 352f)
                .clip(RoundedCornerShape(12f.sx(s))),
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
                color = OtvColors.White, fontSize = 17f.sxs(s), lineHeight = 20f.sxs(s),
                maxLines = 1, overflow = TextOverflow.Ellipsis,
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
            title, color = OtvColors.White, fontSize = 21f.sxs(s), maxLines = 1, overflow = TextOverflow.Ellipsis,
        )
    }
}

/**
 * 需求⑥（2026-09-11）：我的页会员信息——并入「我的收藏」标题行靠右的一行小字
 * （状态点+状态/套餐/到期/剩余），无背景框；**仅行尾带下划线的「续费」两字可聚焦**
 * （旧版整行 dpadFocusable，光标落在整行上；现光标选中环只套「续费」），
 * OK 进会员页（续费/兑换）。仅已激活态渲染（未激活走统一会员页）。
 */
@Composable
private fun MemberInlineInfo(
    s: Float,
    renewFocus: FocusRequester? = null,
    onRedeem: () -> Unit,
) {
    val st by com.qiubo.optimaltv.license.LicenseManager.state.collectAsStateWithLifecycle()
    val d = (st as? com.qiubo.optimaltv.license.LicenseState.Activated)?.data
    val status: String
    val statusColor: Color
    val infoText: String
    when {
        d == null -> { status = "加载中…"; statusColor = OtvColors.White50; infoText = "" }
        d.expiryAt != null && com.qiubo.optimaltv.license.LicenseManager.effNow() >= d.expiryAt -> {
            status = "会员已过期"; statusColor = Color(0xFFFF453A)
            infoText = com.qiubo.optimaltv.ui.paywall.planDisplayName(d.plan) + " · 续费可用"
        }
        !com.qiubo.optimaltv.license.LicenseManager.isPremium() -> {
            status = "等待联网校验"; statusColor = Color(0xFFFF9F0A); infoText = "联网后自动恢复"
        }
        d.expiryAt == null -> { status = "终身会员"; statusColor = Color(0xFF30D158); infoText = "终身 · 永久有效" }
        else -> {
            status = "会员有效"; statusColor = Color(0xFF30D158)
            infoText = com.qiubo.optimaltv.ui.paywall.planDisplayName(d.plan) +
                " · 到期 " + fmtDate(d.expiryAt) +
                " · 剩余 ${com.qiubo.optimaltv.license.LicenseManager.daysLeft()} 天"
        }
    }
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.padding(end = 86f.sx(s)),
    ) {
        Box(
            Modifier
                .size(14f.sx(s))
                .background(statusColor, androidx.compose.foundation.shape.CircleShape),
        )
        Spacer(Modifier.width(12f.sx(s)))
        Text(
            buildString {
                append(status)
                if (infoText.isNotBlank()) append(" · ").append(infoText)
            },
            color = OtvColors.White50,
            fontSize = 22f.sxs(s),
            maxLines = 1,
        )
        // 用户需求（2026-09-05）：行尾带下划线的「续费」入口；
        // 需求⑥（2026-09-11）：只有这两个字可被光标选中
        // 2026-09-19 老板定版（第二轮）：选中效果**改为细白描边框**，去掉之前那套
        // 「白底反色胶囊 + 放大 1.08 + 四周柔和外发光」——
        // 老板原话「换成一个白边的框就好了，细边的框就好了，不用搞那么夸张浮夸」。
        // 现在是：聚焦 = 1.5 设计px 白色圆角描边框 + 文字转白；未聚焦 = 半透明 hairline + 蓝字下划线。
        // 之前那版光晕（6 圈同心描边模拟衰减）整块删除，连同它的 24 设计px 画布边距常量。
        Spacer(Modifier.width(14f.sx(s)))
        var renewFocused by remember { mutableStateOf(false) }
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier
                .then(if (renewFocus != null) Modifier.focusRequester(renewFocus) else Modifier)
                .dpadFocusable(
                    scaleFocused = 1f,     // 不做放大：老板要「不夸张」，聚焦只换描边
                    focusedBg = null,      // 背景自绘（未聚焦态是半透明胶囊）
                    drawRing = false,      // 焦点环也自绘，统一到同一个 border 上
                    onFocusedChange = { renewFocused = it },
                ) { onRedeem() }
                .clip(RoundedCornerShape(50))
                .background(
                    if (renewFocused) OtvColors.White.copy(alpha = 0.06f)   // 极淡填充，只用于垫住白字
                    else Color(0x1FFFFFFF),
                )
                .border(
                    if (renewFocused) 1.5f.sx(s) else 1f.sx(s),
                    if (renewFocused) OtvColors.White else Color(0x33FFFFFF),
                    RoundedCornerShape(50),
                )
                .padding(horizontal = 20f.sx(s), vertical = 9f.sx(s)),
        ) {
            Text(
                "续费",
                color = if (renewFocused) OtvColors.White else AccentBlue,
                fontSize = 22f.sxs(s),
                fontWeight = FontWeight.SemiBold,
                // 聚焦态去掉下划线：白字 + 白框下再压一条下划线会脏
                textDecoration = if (renewFocused) TextDecoration.None else TextDecoration.Underline,
            )
        }
    }
}

/** 收藏+历史区块（我的页两种布局共用）：标题行 + 横滑卡行。titleTop=标题顶部留白（调用方按布局给）；
 *  memberRow 非空时与「我的收藏」标题同行右对齐（需求⑥：会员信息行并入标题行） */
private fun androidx.compose.foundation.lazy.LazyListScope.favHisItems(
    s: Float,
    favItems: List<VodItem>,
    history: List<com.qiubo.optimaltv.data.db.HistoryEntity>,
    contentFocus: FocusRequester,
    cardReg: FocusRegistry,
    nav: androidx.navigation.NavController,
    titleTop: Float,
    updateFocus: FocusRequester,
    memberRow: (@Composable () -> Unit)? = null,
) {
    val hasFav = favItems.isNotEmpty()
    val hasHis = history.isNotEmpty()
    item(key = "fav-title") {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier
                .padding(start = 90f.sx(s), end = 0f.sx(s), top = titleTop.sx(s)),
        ) {
            Text(
                "我的收藏", fontSize = 30f.sxs(s), fontWeight = FontWeight.SemiBold,
                color = OtvColors.White.copy(alpha = 0.92f),
            )
            Spacer(Modifier.weight(1f))
            memberRow?.invoke()
        }
    }
    item(key = "fav-row") {
        if (favItems.isEmpty()) {
            Text(
                "暂无收藏", color = OtvColors.White50, fontSize = 26f.sxs(s),
                modifier = Modifier.padding(start = 90f.sx(s), top = 20f.sx(s)),
            )
        } else {
            val favRowState = rememberLazyListState()
            val favRowScroll = remember(favRowState) { lazyNavContainer(favRowState) }
            ProvideNavScrolls(horizontal = favRowScroll) {
                LazyRow(
                    state = favRowState,
                    horizontalArrangement = Arrangement.spacedBy(24f.sx(s)),
                    contentPadding = PaddingValues(horizontal = 86f.sx(s)),
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
                            focusRequester = if (i == 0) contentFocus else null,
                            cardReg = cardReg,
                            cardKey = favItems[i].id,
                        ) {
                            nav.navigate("detail/" + URLEncoder.encode(favItems[i].id, "UTF-8"))
                        }
                    }
                }
            }
        }
    }
    // 需求 我的#4：观看记录前加「历史记录」标题
    if (hasHis) {
        item(key = "his-title") {
            Text(
                "历史记录", fontSize = 30f.sxs(s), fontWeight = FontWeight.SemiBold,
                color = OtvColors.White.copy(alpha = 0.92f),
                modifier = Modifier.padding(start = 90f.sx(s), top = 44f.sx(s)),
            )
        }
        item(key = "his-row") {
            val hisRowState = rememberLazyListState()
            val hisRowScroll = remember(hisRowState) { lazyNavContainer(hisRowState) }
            ProvideNavScrolls(horizontal = hisRowScroll) {
                LazyRow(
                    state = hisRowState,
                    horizontalArrangement = Arrangement.spacedBy(24f.sx(s)),
                    contentPadding = PaddingValues(horizontal = 86f.sx(s)),
                    modifier = Modifier
                        .padding(top = 22f.sx(s)),
                ) {
                    items(history.take(10).size, key = { i -> history[i].vodId }) { i ->
                        val h = history[i]
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
                            // 需求 我的#1：无收藏时 DOWN 兜底落历史首卡
                            focusRequester = if (!hasFav && i == 0) contentFocus else null,
                            cardReg = cardReg,
                        ) {
                            nav.navigate("player/" + URLEncoder.encode(h.vodId, "UTF-8") + "/${h.epIndex}")
                        }
                    }
                }
            }
        }
    }
    // 2026-09-22：手动「检查更新」入口——遥控 OK 直接查 APK 清单，
    // 结果行内回显（用户报「收不到更新提示」后加的手动通道）。
    item(key = "update-row") { UpdateCheckRow(s, updateFocus) }
}


/**
 * 应用更新行：左侧标题 + 当前版本/检查结果，右侧「检查更新」按钮（唯一可聚焦元素）。
 * 手动检查会忽略「稍后更新」记忆，因此点过稍后的版本也能重新拉起更新弹窗。
 */
@Composable
private fun UpdateCheckRow(s: Float, focusRequester: FocusRequester) {
    val check by com.qiubo.optimaltv.apkupdate.ApkUpdateManager.checkState.collectAsStateWithLifecycle()
    var focused by remember { mutableStateOf(false) }
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .padding(start = 90f.sx(s), end = 86f.sx(s), top = 44f.sx(s), bottom = 16f.sx(s)),
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
                .dpadFocusable(
                    scaleFocused = 1f,
                    focusedBg = null,
                    drawRing = false,
                    externalFocusRequester = focusRequester,
                    onFocusedChange = { focused = it },
                ) { com.qiubo.optimaltv.apkupdate.ApkUpdateManager.checkForUpdatesManually() }
                .clip(RoundedCornerShape(50))
                .background(if (focused) OtvColors.White.copy(alpha = 0.06f) else Color(0x1FFFFFFF))
                .border(
                    if (focused) 1.5f.sx(s) else 1f.sx(s),
                    if (focused) OtvColors.White else Color(0x33FFFFFF),
                    RoundedCornerShape(50),
                )
                .padding(horizontal = 28f.sx(s), vertical = 14f.sx(s)),
        ) {
            Text(
                if (check is com.qiubo.optimaltv.apkupdate.ApkCheckState.Checking) "正在检查…" else "检查更新",
                color = if (focused) OtvColors.White else AccentBlue,
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
    is com.qiubo.optimaltv.apkupdate.ApkCheckState.UpdateFound -> AccentBlue
    is com.qiubo.optimaltv.apkupdate.ApkCheckState.Failed -> Color(0xFFFF9F0A)
    else -> OtvColors.White50
}

private fun fmtDate(ms: Long): String =
    java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.CHINA).format(java.util.Date(ms))

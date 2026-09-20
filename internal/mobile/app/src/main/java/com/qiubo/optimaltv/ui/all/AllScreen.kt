package com.qiubo.optimaltv.ui.all

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.navigation.NavController
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.ui.components.PosterPlaceholder
import com.qiubo.optimaltv.ui.components.tapCard
import com.qiubo.optimaltv.ui.components.MobileBackButton
import com.qiubo.optimaltv.ui.components.MobileBackButtonInset
import com.qiubo.optimaltv.ui.components.MobileBackButtonTouchTarget
import com.qiubo.optimaltv.ui.components.MobileVodCard
import com.qiubo.optimaltv.ui.components.MobileUiTags
import com.qiubo.optimaltv.ui.components.VodFilterRow
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch
import java.net.URLEncoder

/**
 * 全部影视页（原版 view-all，v1.18 布局与 TV 版 1:1 同款）：
 * - 筛选区压缩成紧凑 chips 且随内容滚动（不再固定顶部，需求 我的#7）；
 * - 筛选面板顶边与左上角悬浮返回钮同顶（2026-09-18 用户要求「返回和筛选顶部对齐」），
 *   面板内首行是右侧的「筛选」开关——左侧整块留给返回钮，首行「类别」落在开关下方，
 *   所以同顶也不会被返回钮压住（旧做法是把整个面板下推 64dp 让位，缝太大）；
 * - 筛选组件可收起（2026-09-18 用户要求）：收起只留表头开关，有非默认筛选时开关高亮并显示「已选 N」；
 * - 筛选选项按所选类别拉取（需求 我的#8）；类型/地区/语言/年份行首均加「全部」（需求 我的#9）
 * - 已选项白底黑字（需求 我的#6；触控无聚焦态，选中即白底黑字）
 * - 海报墙 7 列（232×352 r12 + remark 角标 + ★评分）+ 触底自动加载
 * 触控交互：chips/卡片点按；返回键在筛选区外先滚回顶部，再按退出本页。
 */
@Composable
fun AllScreen(nav: NavController) {
    val s = rememberUiScale()
    // 需求12③⑤（2026-09-07 竖屏）：片库页竖屏一行 3 卡 + 筛选堆叠布局 + 顶部让位返回钮
    val configuration = LocalConfiguration.current
    val portrait = configuration.orientation ==
        android.content.res.Configuration.ORIENTATION_PORTRAIT
    // Reference layout keeps the filter and poster grid close to the screen edge on phones;
    // the TV canvas retains the original wider breathing room.
    val sidePad = if (portrait) 24f else 60f
    // Keep the filter card out of the floating back button's visual and touch lane.
    // The rail is drawn over the card's header only; poster columns and filter rows keep their width.
    val filterPanelInset = 36f
    val filterSafeRailWidth = sidePad + filterPanelInset
    // Align the chevron with the first visible chip text ("全部") in both orientations.
    val filterBackOffset = if (portrait) 8f else 16f
    val gridCols = if (portrait) 3 else com.qiubo.optimaltv.ui.theme.rememberRowColumns(
        cardW = 232f, gap = 20f, sidePad = 80f,
    )
    // 筛选状态（默认电影 + 最热排序）；若用户曾切换过类别，则恢复上次选择。
    // 进页先从 DataStore 恢复上次选中的类别（catLoaded 门闸：恢复完成前不发请求）
    var cat by remember { mutableStateOf("1") }
    var type by remember { mutableStateOf("") }
    var area by remember { mutableStateOf("") }
    var year by remember { mutableStateOf("") }
    var rating by remember { mutableStateOf("") }
    var by by remember { mutableStateOf("hot") }
    var catLoaded by remember { mutableStateOf(false) }
    // 片库筛选面板默认展开；折叠状态跨转屏保留，入口只保留图标，避免文字按钮挤占表头。
    var filterOpen by rememberSaveable { mutableStateOf(true) }
    LaunchedEffect(Unit) {
        cat = runCatching { Graph.settings.allLastCat() }.getOrNull().orEmpty().ifBlank { "1" }
        catLoaded = true
    }

    val designRows = VodFilterDesignSpec.rows(portrait)

    // 列表加载：筛选任一变化重置第一页；请求序号防快速切换旧响应覆盖
    var items by remember { mutableStateOf(listOf<VodItem>()) }
    var page by remember { mutableIntStateOf(1) }
    var hasMore by remember { mutableStateOf(false) }
    var loading by remember { mutableStateOf(false) }
    // 上游降级标记（豆瓣被拦/超时）：结果来自缓存或快照时不能显示成「没有符合条件的内容」
    var upstreamAvailable by remember { mutableStateOf(true) }
    val scope = rememberCoroutineScope()
    var reqSeq by remember { mutableIntStateOf(0) }

    LaunchedEffect(cat, type, area, year, rating, by, catLoaded) {
        if (!catLoaded) return@LaunchedEffect
        val seq = ++reqSeq
        loading = true
        val r = Graph.repo.showPage(
            cid = cat.toIntOrNull() ?: 0, type = type, area = area,
            year = year, rating = rating, by = by, page = 1,
        )
        if (seq != reqSeq) return@LaunchedEffect
        items = r?.items.orEmpty().distinctBy { it.id }
        hasMore = r?.hasMore ?: false
        upstreamAvailable = r?.upstreamAvailable ?: true
        page = 1
        loading = false
    }

    fun loadMore() {
        if (loading || !hasMore) return
        loading = true
        val seq = reqSeq
        val next = page + 1
        scope.launch {
            val r = Graph.repo.showPage(
                cid = cat.toIntOrNull() ?: 0, type = type, area = area,
                year = year, rating = rating, by = by, page = next,
            )
            if (seq == reqSeq && r != null) {
                items = (items + r.items).distinctBy { it.id }
                hasMore = r.hasMore
                upstreamAvailable = r.upstreamAvailable
                page = next
                // v1.19 竞态修复：loading 只随本请求代数解锁——旧版无条件清 loading，
                // 筛选切换后旧响应回来会提前解锁，新筛选首屏还在路上就允许触底加载交错上屏
                loading = false
            } else if (seq == reqSeq) {
                loading = false
            }
        }
    }

    // 触底自动加载
    val gridState = rememberLazyGridState()
    LaunchedEffect(gridState, hasMore) {
        snapshotFlow { gridState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0 }
            .distinctUntilChanged()
            .collect { last ->
                val total = gridState.layoutInfo.totalItemsCount
                if (hasMore && !loading && total > 0 && last >= total - 4) loadMore()
            }
    }

    // 返回键：已滚离顶部 → 滚回筛选区；已在顶部 → 退出本页
    val backScope = rememberCoroutineScope()
    BackHandler {
        val atTop = gridState.firstVisibleItemIndex == 0 && gridState.firstVisibleItemScrollOffset == 0
        if (atTop) nav.popBackStack() else backScope.launch { gridState.scrollToItem(0) }
    }
    val activeFilters = listOf(cat, type, area, year, rating).count { it.isNotBlank() } +
        (if (by != "hot") 1 else 0)

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        LazyVerticalGrid(
            state = gridState,
            columns = GridCells.Fixed(gridCols),
            horizontalArrangement = Arrangement.spacedBy((if (portrait) 12f else 20f).sx(s)),
            verticalArrangement = Arrangement.spacedBy((if (portrait) 16f else 24f).sx(s)),
            modifier = Modifier
                .fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(
                // 本页不渲染顶部标签栏（与 TV 版同构），内容占满全屏；竖屏侧距 60→40。
                // 顶边与悬浮返回钮同顶（同为贴边 12dp，2026-09-18 用户要求「返回和筛选顶部对齐」）。
                // 注：返回钮贴边是固定 dp，而设计 px 在窄横屏只缩放到 ~20dp——同顶会让首行
                // 「类别」落进返回钮命中区，所以让位交给面板内的表头（见下方 height(44.dp)）。
                start = sidePad.sx(s), end = sidePad.sx(s),
                top = MobileBackButtonInset,
                bottom = 80f.sx(s),
            ),
        ) {
            // 轻量筛选条：保持现有深色色板，只采用参考图的紧凑节奏。
            item(span = { GridItemSpan(maxLineSpan) }, key = "filters") {
                Column(
                    verticalArrangement = Arrangement.spacedBy(0f.sx(s)),
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(OtvColors.Panel, RoundedCornerShape(18f.sx(s)))
                        .padding(horizontal = 12f.sx(s), vertical = 8f.sx(s))
                ) {
                    Box(
                        contentAlignment = Alignment.CenterEnd,
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(MobileBackButtonTouchTarget),
                    ) {
                        FilterToggle(
                            open = filterOpen,
                            activeCount = activeFilters,
                            s = s,
                            onToggle = { filterOpen = !filterOpen },
                        )
                    }
                    if (filterOpen) {
                        designRows.forEach { row ->
                            val selected = when (row.key) {
                                "category" -> cat
                                "genre" -> type
                                "region" -> area
                                "year" -> year
                                "rating" -> rating
                                else -> by
                            }
                            VodFilterRow(
                                row.label,
                                row.options.map { it.value to it.label },
                                selected,
                                s,
                                portrait,
                                rowHeight = if (portrait) {
                                    VodFilterDesignSpec.compactLayout.mobileRowHeight
                                } else {
                                    VodFilterDesignSpec.compactLayout.tvRowHeight
                                },
                                chipHeight = if (portrait) {
                                    VodFilterDesignSpec.compactLayout.mobileChipHeight
                                } else {
                                    VodFilterDesignSpec.compactLayout.chipHeight
                                },
                                textLineHeight = if (portrait) {
                                    VodFilterDesignSpec.compactLayout.mobileTextLineHeight
                                } else {
                                    VodFilterDesignSpec.compactLayout.tvTextLineHeight
                                },
                                includeFontPadding = VodFilterDesignSpec.compactLayout.includeFontPadding,
                            ) { value ->
                                when (row.key) {
                                    "category" -> {
                                        cat = value
                                        type = ""; area = ""; year = ""; rating = ""
                                        Graph.appScope.launch { runCatching { Graph.settings.rememberAllLastCat(value) } }
                                    }
                                    "genre" -> type = value
                                    "region" -> area = value
                                    "year" -> year = value
                                    "rating" -> rating = value
                                    "sort" -> by = value
                                }
                            }
                        }
                    }
                }
            }
            // 上游降级：有内容时说「可能不是最新」，没内容时说「暂时获取不到」——
            // 绝不把上游故障显示成「没有符合条件的内容」。
            if (!upstreamAvailable) {
                item(span = { GridItemSpan(maxLineSpan) }, key = "upstream") {
                    Text(
                        if (items.isEmpty()) "暂时获取不到片库，请稍后重试"
                        else "数据可能不是最新，正在重试…",
                        color = OtvColors.White50, fontSize = 22f.sxs(s),
                        modifier = Modifier.padding(top = 4f.sx(s)),
                    )
                }
            }
            items(items, key = { it.id }) { item ->
                VcardCard(nav, item, s, compact = portrait)
            }
            if (loading) {
                item(span = { GridItemSpan(maxLineSpan) }, key = "loading") {
                    Text("加载中…", color = OtvColors.White50, fontSize = 24f.sxs(s))
                }
            } else if (hasMore) {
                item(span = { GridItemSpan(maxLineSpan) }, key = "more") {
                    Box(
                        contentAlignment = Alignment.Center,
                        modifier = Modifier
                            .clip(RoundedCornerShape(28f.sx(s)))
                            .tapCard { loadMore() }
                            .background(OtvColors.ChipBg, RoundedCornerShape(28f.sx(s)))
                            .padding(horizontal = 44f.sx(s), vertical = 12f.sx(s)),
                    ) {
                        Text(
                            "加载更多…",
                            color = OtvColors.White,
                            fontSize = 24f.sxs(s), fontWeight = FontWeight.SemiBold,
                        )
                    }
                }
            }
        }
        // Reserve the header lane without painting over the card: the panel's rounded corner
        // must stay continuous behind the smaller back icon.
        Box(
            modifier = Modifier
                .width(filterSafeRailWidth.sx(s))
                .height(MobileBackButtonInset + MobileBackButtonTouchTarget)
                .background(Color.Transparent),
        )
        // 移动① 修复：浮层按钮放 Box 最后一个子节点（最上层）——先声明会被
        // LazyVerticalGrid 整面拦截触摸（网格自身可滚动，吃掉全部落点）
        MobileBackButton(
            modifier = Modifier.offset(x = filterBackOffset.sx(s)),
            iconSize = 22.dp,
            onBack = { nav.popBackStack() },
        )
    }
}

/**
 * 移动端筛选入口：用三条滑杆图标替代「筛选⌄」文字按钮。
 * 默认状态是深色方形，存在非默认条件时切为白底并显示数量角标；
 * 展开/收起状态保留在无障碍 stateDescription 中，不再绘制拥挤的小箭头。
 */
@Composable
private fun FilterToggle(
    open: Boolean,
    activeCount: Int,
    s: Float,
    onToggle: () -> Unit,
) {
    val active = activeCount > 0
    // The icon floats directly on the panel; only the small count badge communicates an active filter.
    val buttonBg = Color.Transparent
    val iconFg = OtvColors.White75
    val shape = RoundedCornerShape(14f.sx(s))
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            // Header padding is 8 design px; cancel it so this button shares the
            // same visual center line as MobileBackButton's 12dp inset + 44dp target.
            .offset(y = (-8f).sx(s))
            .size(MobileBackButtonTouchTarget)
            .clip(shape)
            .testTag(MobileUiTags.FilterToggle)
            .semantics {
                role = Role.Button
                contentDescription = if (active) "筛选（已选 $activeCount）" else "筛选"
                stateDescription = if (open) "已展开" else "已收起"
            }
            .tapCard(onToggle)
            .background(buttonBg, shape),
    ) {
        Canvas(Modifier.size(24f.sx(s))) {
            val stroke = 1.8f.sx(s).toPx()
            val left = size.width * 0.12f
            val right = size.width * 0.88f
            val radius = size.minDimension * 0.13f
            val rows = listOf(
                size.height * 0.20f to size.width * 0.72f,
                size.height * 0.50f to size.width * 0.34f,
                size.height * 0.80f to size.width * 0.60f,
            )
            rows.forEach { (y, knobX) ->
                drawLine(
                    color = iconFg,
                    start = androidx.compose.ui.geometry.Offset(left, y),
                    end = androidx.compose.ui.geometry.Offset(right, y),
                    strokeWidth = stroke,
                    cap = StrokeCap.Round,
                )
                drawCircle(buttonBg, radius, androidx.compose.ui.geometry.Offset(knobX, y))
                drawCircle(
                    color = iconFg,
                    radius = radius,
                    center = androidx.compose.ui.geometry.Offset(knobX, y),
                    style = Stroke(width = stroke),
                )
            }
        }
        if (active) {
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier
                    .align(Alignment.TopEnd)
                    .offset(x = (-4f).sx(s), y = 4f.sx(s))
                    .size(16f.sx(s))
                    .background(OtvColors.Bg, CircleShape),
            ) {
                Text(
                    if (activeCount > 9) "9+" else activeCount.toString(),
                    color = OtvColors.White,
                    fontSize = 9f.sxs(s),
                    fontWeight = FontWeight.Bold,
                    maxLines = 1,
                )
            }
        }
    }
}

/**
 * 紧凑筛选行：标签 21px + 小号 chips。
 * 需求 影视#10：胶囊高度降为原来的 2/3（字号 19→17、上下内边距 8→3）。
 * 需求③：选中项白底黑字（触控无聚焦态，选中即白底黑字）。
 * 需求12③（2026-09-07 竖屏）：竖屏改常规竖屏布局——标签独占一行置顶，
 * 选项换行铺开（FlowRow）不再横向滚动藏选项；chips 触控区加大。
 */
/** vcard（原版：poster 232×352 r12 + remark 角标 + vname 24 + vmeta 20 .5白；触控点卡进详情）
 *  竖屏优化：fillMaxWidth 填满网格 cell（1fr 均分，与网页版 poster-grid 同构） */
@Composable
private fun VcardCard(
    nav: NavController,
    item: VodItem,
    s: Float,
    compact: Boolean,
) {
    MobileVodCard(
        item = item,
        s = s,
        compact = compact,
        onClick = { nav.navigate("detail/" + URLEncoder.encode(item.id, "UTF-8")) },
    )
}

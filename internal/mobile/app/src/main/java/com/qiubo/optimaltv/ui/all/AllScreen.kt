package com.qiubo.optimaltv.ui.all

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
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
import com.qiubo.optimaltv.data.source.HhkanSource
import com.qiubo.optimaltv.ui.components.MobileUiTags
import com.qiubo.optimaltv.ui.components.PosterPlaceholder
import com.qiubo.optimaltv.ui.components.tapCard
import com.qiubo.optimaltv.ui.components.MobileBackButton
import com.qiubo.optimaltv.ui.components.MobileBackButtonInset
import com.qiubo.optimaltv.ui.components.MobileBackButtonTouchTarget
import com.qiubo.optimaltv.ui.components.MobileVodCard
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
    val portrait = androidx.compose.ui.platform.LocalConfiguration.current.orientation ==
        android.content.res.Configuration.ORIENTATION_PORTRAIT
    val sidePad = if (portrait) 40f else 60f
    val gridCols = if (portrait) 3 else com.qiubo.optimaltv.ui.theme.rememberRowColumns(
        cardW = 232f, gap = 20f, sidePad = 80f,
    )
    // 筛选状态（原版 allState：默认全不选 + 最热排序）。
    // 进页先从 DataStore 恢复上次选中的类别（catLoaded 门闸：恢复完成前不发请求）
    var cat by remember { mutableStateOf("") }
    var type by remember { mutableStateOf("") }
    var area by remember { mutableStateOf("") }
    var year by remember { mutableStateOf("") }
    var rating by remember { mutableStateOf("") }
    var by by remember { mutableStateOf("hot") }
    var catLoaded by remember { mutableStateOf(false) }
    // 筛选面板收起态（2026-09-18 用户要求「筛选组件要能够收起」）：默认展开，
    // 页面被系统回收/转屏后保持用户选择（rememberSaveable），不影响筛选值本身。
    var filterOpen by rememberSaveable { mutableStateOf(true) }
    LaunchedEffect(Unit) {
        cat = runCatching { Graph.settings.allLastCat() }.getOrNull() ?: ""
        catLoaded = true
    }

    // 筛选选项按类别拉取；切类别时清空其余筛选并重载（需求 我的#8）
    // v1.20 修复（移动③根因之一）：filters 拉取失败被 runCatching 吞掉后界面只剩
    // 「全部」chips（用户报「筛选不生效」）——空结果自动重试（后端冷启动竞态自愈）
    var filters by remember { mutableStateOf(HhkanSource.FilterOptions()) }
    val catId = cat.toIntOrNull() ?: 1
    LaunchedEffect(catId, catLoaded) {
        if (!catLoaded) return@LaunchedEffect
        type = ""; area = ""; year = ""; rating = ""
        var attempt = 0
        while (attempt < 4) {
            filters = Graph.repo.allFilters(catId)
            if (filters.types.isNotEmpty()) break
            attempt++
            kotlinx.coroutines.delay(2500L)
        }
    }

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

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        LazyVerticalGrid(
            state = gridState,
            columns = GridCells.Fixed(gridCols),
            horizontalArrangement = Arrangement.spacedBy((if (portrait) 12f else 20f).sx(s)),
            verticalArrangement = Arrangement.spacedBy(30f.sx(s)),
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
            // 筛选区（随内容滚动；「轨」样式 2026-09-19 设计稿 v2 方案①：面板白 5% 底、
            // 行间 1px 发丝分隔、裸文字选项 + 选中白胶囊；竖屏=标签置顶+选项换行铺开，需求12③）
            item(span = { GridItemSpan(maxLineSpan) }, key = "filters") {
                Column(
                    verticalArrangement = Arrangement.spacedBy(0f.sx(s)),
                    modifier = Modifier
                        .background(Color(0x0DFFFFFF), RoundedCornerShape(18f.sx(s)))
                        .padding(horizontal = 20f.sx(s), vertical = 10f.sx(s))
                        .fillMaxWidth(),
                ) {
                    // 表头：只有右侧的收起/展开开关（左侧整块留给悬浮返回钮，开关靠右就不抢地方）；
                    // 高度取返回钮触控命中区——面板顶边与返回钮同顶后，首行「类别」仍落在它下面，
                    // 横屏（设计 px 缩放后可能低于返回钮底边）与竖屏都不会被压住。
                    val activeFilters = listOf(type, area, year, rating).count { it.isNotBlank() } +
                        (if (by != "hot") 1 else 0) + (if (cat.isNotBlank()) 1 else 0)
                    Box(
                        contentAlignment = Alignment.CenterEnd,
                        modifier = Modifier.fillMaxWidth().height(MobileBackButtonTouchTarget),
                    ) {
                        FilterToggle(open = filterOpen, activeCount = activeFilters, s = s) {
                            filterOpen = !filterOpen
                        }
                    }
                    if (filterOpen) {
                        // 「轨」行分隔：1px 白 5% 发丝线（首行不画）
                        @Composable
                        fun RowDivider() = Box(
                            Modifier
                                .fillMaxWidth()
                                .padding(horizontal = if (portrait) 24f.sx(s) else 0f.sx(s))
                                .height(1f.sx(s))
                                .background(Color(0x0DFFFFFF)),
                        )
                        val catChips = listOf("" to "全部", "1" to "电影", "2" to "电视剧", "3" to "动漫", "4" to "综艺", "6" to "短剧")
                        VodFilterRow("类别", catChips, cat, s, portrait) { value ->
                            cat = value
                            Graph.appScope.launch { runCatching { Graph.settings.rememberAllLastCat(value) } }
                        }
                        RowDivider()
                        VodFilterRow("类型", listOf("" to "全部") + filters.types.filter { it != "全部" }.map { it to it }, type, s, portrait) { type = it }
                        RowDivider()
                        VodFilterRow("地区", listOf("" to "全部") + filters.areas.filter { it != "全部" }.map { it to it }, area, s, portrait) { area = it }
                        RowDivider()
                        VodFilterRow("年份", listOf("" to "全部") + filters.years.filter { it != "全部" }.map { it to it }, year, s, portrait) { year = it }
                        RowDivider()
                        VodFilterRow("评分", listOf("" to "全部") + filters.ratings.filter { it != "全部" }.map { it to it }, rating, s, portrait) { rating = it }
                        RowDivider()
                        VodFilterRow("排序", listOf("hot" to "热门", "new" to "最新上映", "rating" to "豆瓣高分"), by, s, portrait) { by = it }
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
        // 移动① 修复：浮层按钮放 Box 最后一个子节点（最上层）——先声明会被
        // LazyVerticalGrid 整面拦截触摸（网格自身可滚动，吃掉全部落点）
        MobileBackButton(onBack = { nav.popBackStack() })
    }
}

/**
 * 紧凑筛选行：标签 21px + 小号 chips。
 * 需求 影视#10：胶囊高度降为原来的 2/3（字号 19→17、上下内边距 8→3）。
 * 需求③：选中项白底黑字（触控无聚焦态，选中即白底黑字）。
 * 需求12③（2026-09-07 竖屏）：竖屏改常规竖屏布局——标签独占一行置顶，
 * 选项换行铺开（FlowRow）不再横向滚动藏选项；chips 触控区加大。
 */
/**
 * 筛选面板的收起/展开开关（2026-09-18 用户要求「筛选组件要能够收起」）。
 * 收起后仍要有「筛选生效中」的可见线索：有非默认筛选时开关白底高亮并显示「已选 N」，
 * 否则用户收起了面板又看不到自己选了什么。箭头方向与网页版 `#filterToggle` 同款
 * （展开 = ⌄，收起 = 旋转 180°）。
 */
@Composable
private fun FilterToggle(
    open: Boolean,
    activeCount: Int,
    s: Float,
    onToggle: () -> Unit,
) {
    val active = activeCount > 0
    val fg = if (active) OtvColors.Bg else OtvColors.White75
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6f.sx(s)),
        modifier = Modifier
            .clip(RoundedCornerShape(15f.sx(s)))
            .testTag(MobileUiTags.FilterToggle)
            .semantics {
                role = Role.Button
                stateDescription = if (open) "已展开" else "已收起"
            }
            .tapCard { onToggle() }
            .background(if (active) OtvColors.White else OtvColors.ChipBg, RoundedCornerShape(15f.sx(s)))
            .padding(horizontal = 16f.sx(s), vertical = 8f.sx(s)),
    ) {
        Text(
            if (active) "筛选（已选 $activeCount）" else "筛选",
            color = fg, fontSize = 17f.sxs(s), fontWeight = FontWeight.SemiBold, maxLines = 1,
        )
        Text(
            "⌄",
            color = fg, fontSize = 17f.sxs(s), fontWeight = FontWeight.SemiBold,
            modifier = Modifier.graphicsLayer(rotationZ = if (open) 0f else 180f),
        )
    }
}

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

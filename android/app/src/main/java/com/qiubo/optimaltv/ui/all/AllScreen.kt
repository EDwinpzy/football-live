package com.qiubo.optimaltv.ui.all

import androidx.activity.compose.BackHandler
import androidx.compose.animation.animateColorAsState
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.navigation.NavController
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.data.source.HhkanSource
import com.qiubo.optimaltv.ui.components.FocusRegistry
import com.qiubo.optimaltv.ui.components.PosterPlaceholder
import com.qiubo.optimaltv.ui.components.ProvideNavScrolls
import com.qiubo.optimaltv.ui.components.ReturnFocus
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.components.lazyGridNavContainer
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch
import java.net.URLEncoder

/**
 * 全部影视页（原版 view-all，2026-08-28 需求重排）：
 * - 筛选区压缩成紧凑 chips 且随内容滚动（不再固定顶部，需求 我的#7）；
 *   选中下方卡片按返回 → 回到筛选区（聚焦首行，需求 我的#7）
 * - 筛选选项按所选类别拉取（需求 我的#8，旧版固定电影频道选项导致结果与源站不一致）；
 *   类型/地区/语言/年份行首均加「全部」（需求 我的#9）；类别行无「全部」（需求⑯）
 * - chips 聚焦才白底黑字；已选项不再常显白底（需求 我的#6）
 * - 海报墙 7 列（232×352 r12 + remark 角标 + ★评分）+ 触底自动加载
 */
@Composable
fun AllScreen(nav: NavController) {
    val s = rememberUiScale()
    val firstChipFocus = remember { FocusRequester() }
    // v1.16：上次选中类别 chip 的落焦句柄（进页初始焦点 = 选中的类别，不再回「全部」）
    val catChipFocus = remember { FocusRequester() }
    // 返回落焦（需求 影视#6-2）：从详情页返回时回到上次聚焦的影片卡
    val returnKey = remember { ReturnFocus.take("all") }
    val cardReg = remember { FocusRegistry() }
    // 首帧落焦：有返回记录 → 卡片；否则 → 类别行选中 chip
    var didInitialFocus by remember { mutableStateOf(false) }
    // 筛选状态（原版 allState：默认全不选 + 最热排序）。
    // v1.16：进页先从 DataStore 恢复上次选中的类别（catLoaded 门闸：恢复完成前不发请求）。
    // catLoaded 须先于初始落焦 effect 声明（Kotlin 局部变量先声明后使用）
    var cat by remember { mutableStateOf("") }
    var type by remember { mutableStateOf("") }
    var area by remember { mutableStateOf("") }
    var year by remember { mutableStateOf("") }
    var rating by remember { mutableStateOf("") }
    var by by remember { mutableStateOf("hot") }
    var catLoaded by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        if (didInitialFocus) return@LaunchedEffect
        didInitialFocus = true
        val key = returnKey
        if (key == null) {
            // v1.16：必须等「上次类别」载入（catLoaded）后 catChipFocus 才挂到选中 chip 上——
            // 否则初始焦点先落到「全部」chip（实测竞态），违背「进页强制选中上次类别」
            var waited = 0
            while (!catLoaded && waited < 3000) { kotlinx.coroutines.delay(50); waited += 50 }
            repeat(60) {
                if (runCatching { catChipFocus.requestFocus() }.isSuccess) return@LaunchedEffect
                kotlinx.coroutines.delay(50)
            }
            runCatching { firstChipFocus.requestFocus() }
        } else {
            repeat(60) {
                if (runCatching { cardReg.fr(key).requestFocus() }.isSuccess) return@LaunchedEffect
                kotlinx.coroutines.delay(50)
            }
            runCatching { catChipFocus.requestFocus() }
        }
    }
    LaunchedEffect(Unit) {
        // 需求⑯（2026-09-11）：类别行去掉「全部」chip（cid=0 全站合并查询是 5 频道并行
        // 抓取的最慢路径）；无记忆/记忆为空（旧版存过「全部」）时默认「电影」
        cat = (runCatching { Graph.settings.allLastCat() }.getOrNull() ?: "").ifBlank { "1" }
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

    // 返回键（需求 我的#7）：焦点在海报卡上 → 滚回筛选区并聚焦首行；已在筛选区 → 退出本页
    var filterFocused by remember { mutableStateOf(false) }
    // 需求 影视#9：光标往上回到筛选行时页面同步滚回最上方（否则筛选区顶部被裁切）
    fun filterFocusIn() {
        filterFocused = true
        scope.launch { gridState.scrollToItem(0) }
    }
    val backScope = rememberCoroutineScope()
    BackHandler {
        if (filterFocused) {
            nav.popBackStack()
        } else {
            backScope.launch { gridState.scrollToItem(0) }
            runCatching { firstChipFocus.requestFocus() }
        }
    }

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        val gridScroll = remember(gridState) { lazyGridNavContainer(gridState) }
        ProvideNavScrolls(vertical = gridScroll) {
        LazyVerticalGrid(
            state = gridState,
            columns = GridCells.Fixed(7),
            horizontalArrangement = Arrangement.spacedBy(20f.sx(s)),
            verticalArrangement = Arrangement.spacedBy(30f.sx(s)),
            modifier = Modifier
                .fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(
                // 需求追加：本页不再渲染顶部标签栏，内容占满全屏（顶部仅留少量呼吸留白）
                start = 60f.sx(s), end = 60f.sx(s), top = 36f.sx(s), bottom = 80f.sx(s),
            ),
        ) {
        // 筛选区（随内容滚动；「轨」样式 2026-09-19 设计稿 v2 方案①：面板白 5% 底、
        // 行间 1px 发丝分隔、裸文字选项 + 选中白胶囊、焦点白环不落底）
        item(span = { GridItemSpan(maxLineSpan) }, key = "filters") {
            Column(
                verticalArrangement = Arrangement.spacedBy(0f.sx(s)),
                modifier = Modifier
                    .background(Color(0x0DFFFFFF), RoundedCornerShape(18f.sx(s)))
                    .padding(horizontal = 16f.sx(s), vertical = 6f.sx(s))
                    .fillMaxWidth(),
            ) {
                    // 「轨」行分隔：1px 白 5% 发丝线（首行不画）
                    @Composable
                    fun RowDivider() = Box(
                        Modifier.fillMaxWidth().height(1f.sx(s)).background(Color(0x0DFFFFFF)),
                    )
                    // 需求⑯（2026-09-11）：类别行去掉「全部」——默认/兜底都是电影（cid=1）；
                    // 类型/地区/语言/年份各行的「全部」保留（是清除对应筛选的唯一途径）
                    val catChips = listOf("1" to "电影", "2" to "电视剧", "3" to "动漫", "4" to "综艺", "6" to "短剧")
                    FilterRow(
                        "类别", catChips, cat, s, firstChipFocus,
                        // v1.16：进页初始焦点落在「上次选中的类别」chip 上
                        selectedChipFocus = catChipFocus,
                        onFocusIn = { filterFocused = true },
                        onFocusOut = { filterFocused = false },
                    ) { value ->
                        cat = value
                        Graph.appScope.launch { runCatching { Graph.settings.rememberAllLastCat(value) } }
                    }
                    RowDivider()
                    FilterRow("类型", listOf("" to "全部") + filters.types.filter { it != "全部" }.map { it to it }, type, s, null,
                        onFocusIn = { filterFocused = true },
                        onFocusOut = { filterFocused = false },
                        onSelect = { type = it })
                    RowDivider()
                    FilterRow("地区", listOf("" to "全部") + filters.areas.filter { it != "全部" }.map { it to it }, area, s, null,
                        onFocusIn = { filterFocused = true },
                        onFocusOut = { filterFocused = false },
                        onSelect = { area = it })
                    RowDivider()
                    // 年份含「更早」（原版 (d.years||[]).concat(['更早'])）
                    FilterRow("年份", listOf("" to "全部") + filters.years.filter { it != "全部" }.map { it to it }, year, s, null,
                        onFocusIn = { filterFocused = true },
                        onFocusOut = { filterFocused = false },
                        onSelect = { year = it })
                    RowDivider()
                    FilterRow("评分", listOf("" to "全部") + filters.ratings.filter { it != "全部" }.map { it to it }, rating, s, null,
                        onFocusIn = { filterFocused = true },
                        onFocusOut = { filterFocused = false },
                        onSelect = { rating = it })
                    RowDivider()
                    FilterRow("排序", listOf("hot" to "热门", "new" to "最新上映", "rating" to "豆瓣高分"), by, s, null,
                        onFocusIn = { filterFocused = true },
                        onFocusOut = { filterFocused = false },
                        onSelect = { by = it })
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
                VcardCard(nav, item, s, cardReg)
            }
            if (loading) {
                item(span = { GridItemSpan(maxLineSpan) }, key = "loading") {
                    Text("加载中…", color = OtvColors.White50, fontSize = 24f.sxs(s))
                }
            } else if (hasMore) {
                item(span = { GridItemSpan(maxLineSpan) }, key = "more") {
                    var moreFocused by remember { mutableStateOf(false) }
                    Box(
                        contentAlignment = Alignment.Center,
                        modifier = Modifier
                            .dpadFocusable(
                                scaleFocused = 1.04f, focusedBg = OtvColors.White,
                                focusedBgRadius = 28f.sx(s),
                                onFocusedChange = { moreFocused = it },
                            ) { loadMore() }
                            .background(OtvColors.ChipBg, RoundedCornerShape(28f.sx(s)))
                            .padding(horizontal = 44f.sx(s), vertical = 12f.sx(s)),
                    ) {
                        Text(
                            "加载更多…",
                            color = if (moreFocused) OtvColors.Bg else OtvColors.White,
                            fontSize = 24f.sxs(s), fontWeight = FontWeight.SemiBold,
                        )
                    }
                }
            }
        }
        } // ProvideNavScrolls(gridScroll)
        // 需求追加：本页不渲染顶部标签栏（足球/影视/我的/搜索）——内容占满全屏；
        // 方向键本来就进不了纯展示 tab，删除后无行为差异
    }
}

/**
 * 「轨」筛选行（2026-09-19 设计稿 v2 方案①）：label 18px 白 35% + 裸文字选项。
 * 选中 = 白胶囊黑字加粗（200ms 颜色渐变）；聚焦 = 白环 + 文字转白 + scale 1.04（不落白底），
 * 与选中态一眼可分；聚焦落在已选项上时仍是白胶囊（dpadFocusable 亮色底自动免环）。
 */
@Composable
private fun FilterRow(
    label: String,
    chips: List<Pair<String, String>>,
    selected: String,
    s: Float,
    firstChipFocus: FocusRequester?,
    onFocusIn: () -> Unit,
    onFocusOut: () -> Unit,
    /** v1.16：挂到「当前选中」chip 上的落焦句柄（进页初始焦点 = 上次选中类别） */
    selectedChipFocus: FocusRequester? = null,
    onSelect: (String) -> Unit,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.padding(vertical = 4f.sx(s)),
    ) {
        Text(
            label, color = OtvColors.White.copy(alpha = 0.35f), fontSize = 17f.sxs(s), fontWeight = FontWeight.Medium,
            modifier = Modifier.width(52f.sx(s)),
        )
        Spacer(Modifier.width(8f.sx(s)))
        Row(
            horizontalArrangement = Arrangement.spacedBy(2f.sx(s)),
            modifier = Modifier.horizontalScroll(rememberScrollState()),
        ) {
            chips.forEachIndexed { i, (value, name) ->
                val active = selected == value
                var focused by remember { mutableStateOf(false) }
                val chipBg by animateColorAsState(
                    if (active) OtvColors.White else Color.Transparent, label = "filterChipBg",
                )
                val chipFg by animateColorAsState(
                    if (active) OtvColors.Bg else if (focused) OtvColors.White else OtvColors.White55,
                    label = "filterChipFg",
                )
                Box(
                    contentAlignment = Alignment.Center,
                    modifier = Modifier
                        .then(if (i == 0 && firstChipFocus != null) Modifier.focusRequester(firstChipFocus) else Modifier)
                        .then(if (active && selectedChipFocus != null) Modifier.focusRequester(selectedChipFocus) else Modifier)
                        .dpadFocusable(
                            scaleFocused = 1.04f,
                            // 焦点不再画白底（「轨」焦点=白环）；仅当 chip 已选中时补白胶囊
                            //（选中胶囊由下方 background 绘制，此处同铺一层保持旧视觉链路）
                            focusedBg = if (active) OtvColors.White else null,
                            focusedBgRadius = 20f.sx(s),
                            onFocusedChange = {
                                focused = it
                                if (it) onFocusIn() else onFocusOut()
                            },
                        ) { onSelect(value) }
                        .background(chipBg, RoundedCornerShape(percent = 50))
                        .padding(horizontal = 12f.sx(s), vertical = 4f.sx(s)),
                ) {
                    Text(
                        name,
                        color = chipFg,
                        fontSize = 16f.sxs(s),
                        fontWeight = if (active) FontWeight.Bold else FontWeight.Medium,
                        maxLines = 1,
                    )
                }
            }
        }
    }
}

/** vcard（原版：poster 232×352 r12 + remark 角标 + vname 24 + vmeta 20 .5白） */
@Composable
private fun VcardCard(
    nav: NavController,
    item: VodItem,
    s: Float,
    cardReg: FocusRegistry? = null,
) {
    Column(Modifier.width(232f.sx(s))) {
        Box(
            Modifier
                .dpadFocusable(
                    scaleFocused = 1.05f,
                    onFocusedChange = { if (it) ReturnFocus.mark("all", item.id) },
                    externalFocusRequester = cardReg?.fr(item.id),
                ) {
                    nav.navigate("detail/" + URLEncoder.encode(item.id, "UTF-8"))
                }
                .fillMaxWidth()
                .aspectRatio(232f / 352f)
                .clip(RoundedCornerShape(12f.sx(s))),
        ) {
            PosterPlaceholder(seed = item.id.hashCode(), modifier = Modifier.fillMaxSize())
            if (item.posterUrl.isNotBlank()) {
                AsyncImage(
                    model = item.posterUrl,
                    contentDescription = item.title,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxSize(),
                )
            }
            if (item.remark.isNotBlank()) {
                Text(
                    item.remark,
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
        Text(item.title, color = OtvColors.White, fontSize = 24f.sxs(s), maxLines = 1, overflow = TextOverflow.Ellipsis)
        // 2026-09-17 用户要求：卡片下方不再显示豆瓣评分（与影视页片库卡保持一致）
    }
}

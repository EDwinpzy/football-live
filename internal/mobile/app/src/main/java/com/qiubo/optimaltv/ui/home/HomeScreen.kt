package com.qiubo.optimaltv.ui.home

import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.spring
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.scrollBy
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
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.PagerDefaults
import androidx.compose.foundation.pager.PagerSnapDistance
import androidx.compose.foundation.pager.VerticalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavController
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.db.HistoryEntity
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.data.model.formatTime
import com.qiubo.optimaltv.ui.components.AppleLoading
import com.qiubo.optimaltv.ui.components.MainTabBar
import com.qiubo.optimaltv.ui.components.MobileVodCard
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.PosterPlaceholder
import com.qiubo.optimaltv.ui.components.VodCategorySelector
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
 * 影视页（原版 view-vod，v1.18 与 TV 版 1:1 同款布局）：
 * 整页单 LazyColumn——首项 = hero 海报带（VOD_BAND 高：海报顶到屏幕最顶不留白 + 半透明白雾），
 * 之后 = 最近观看 + 片库（分类 chips + 网格行，每行 7 张竖卡 232×352 设计px）。
 * 与 TV 版唯一差异：交互触控（hero 横滑/点圆点翻页、卡片/chip 点按，无 D-pad 聚焦态）；
 * 顶栏随内容滚动下潜：内容离开绝对顶部即隐藏，回到顶部显示。
 * 需求⑤（2026-09-04）：竖屏 hero 带缩短（700→520 设计px）让轮播+最近观看整体上移。
 * 2026-09-08 竖屏适配：核心分类 3×2 全量可见；横屏保持单行，避免把入口藏在横向手势里。
 */
private const val VOD_BAND = 700f
private const val VOD_BAND_PORTRAIT = 520f

/**
 * hero 各文字块的高度上限（设计值 + 4px 余量）：title 64 / meta 54 / desc 74。
 * 2026-09-19 修复（用户报障「奥德赛」立刻播放钮整颗消失）：截图实测设备上 title/meta
 * 的文字框比设计高约 90px（字体行高差异），三行累计把尾部按钮顶出海报带、meta 与简介
 * 也整体下移。逐块封顶后，异常设备的行高被夹回设计值；与 TV 版同口径。
 */
private const val VOD_HERO_TITLE_MAX = 68f
private const val VOD_HERO_META_MAX = 58f
private const val VOD_HERO_DESC_MAX = 78f

/** hero 文字列总上限（= 68 + 8 + 58 + 10 + 78 = 222），总闸，兜住任何单块漏网 */
private const val VOD_HERO_TEXT_MAX = 222f

/** hero 播放钮距海报带底部（设计钮框：横屏 y576..640/带 700 → 60；竖屏 y406..470/带 520 → 50） */
private const val VOD_HERO_BTN_BOTTOM_LAND = 60f
private const val VOD_HERO_BTN_BOTTOM_PORT = 50f

/** Keep the mobile library page order aligned with the TV home library pager. */
private val MOBILE_LIBRARY_SECTION_TITLES = listOf("最近热门", "最近更新", "豆瓣高分")
private val MOBILE_LIBRARY_SECTION_SOURCE_ALIASES = listOf(
    listOf("最近热门", "近期热门"),
    listOf("最近更新", "最新上映", "最新上线"),
    listOf("豆瓣高分"),
)

@Composable
@OptIn(ExperimentalFoundationApi::class)
fun VodScreen(nav: NavController) {
    val catalogState by Graph.repo.state.collectAsStateWithLifecycle()
    val historyFlow = remember { Graph.db.vodDao().historyFlow(12) }
    val history by historyFlow.collectAsStateWithLifecycle(initialValue = emptyList())
    val s = rememberUiScale()
    val portrait = androidx.compose.ui.platform.LocalConfiguration.current.orientation ==
        android.content.res.Configuration.ORIENTATION_PORTRAIT
    // 竖屏优化：全页侧距 80/86→40（与 hero 内容 start40 及网页版 portrait 40 同档）
    val homePad = (if (portrait) 40f else 80f).sx(s)

    // hero 数据（原版 refreshVod 降级链：源站轮播 carousel≥3 优先 → 首页热门板块合并 → 目录前 6 兜底）
    val home by Graph.repo.homeFlow.collectAsStateWithLifecycle()
    /* 2026-09-18：进页兜底——目录为空（后端冷启动/热更重启期首轮抓取全失败）时补抓，
       否则 hero 会退化成空底板、分类卡与三栏全空。 */
    LaunchedEffect(Unit) {
        Graph.repo.ensureCatalogFresh()
        Graph.repo.refreshHome()
    }
    val cat0 = catalogState.catalog
    val heroesTop = remember(cat0, home) { buildHeroes(cat0, home) }

    // 返回：内容区先回顶；已在顶部双击退出（需求⑥，首按轻提示）
    val vodListState = rememberLazyListState()
    val backScope = androidx.compose.runtime.rememberCoroutineScope()
    var hint by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(hint) { if (hint != null) { kotlinx.coroutines.delay(2500); hint = null } }
    // 2026-09-19：顶部再按返回 → 双击退出（首次只轻提示），走与 TV 版同一条
    // ExitCoordinator 退出链；内容区返回仍先滚回顶部
    val exitApp = com.qiubo.optimaltv.ui.components.rememberDoubleBackExit { hint = it }
    BackHandler {
        val atTop = vodListState.firstVisibleItemIndex == 0 && vodListState.firstVisibleItemScrollOffset == 0
        if (atTop) exitApp() else backScope.launch { vodListState.scrollToItem(0) }
    }
    // 顶栏下潜：内容离开绝对顶部即隐藏、回顶显示
    val chromeGone by remember(vodListState) {
        derivedStateOf {
            isScrolledFromTop(
                vodListState.firstVisibleItemIndex,
                vodListState.firstVisibleItemScrollOffset,
            )
        }
    }

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        val cat = catalogState.catalog
        when {
            catalogState.loading && cat == null -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                AppleLoading()
            }
            cat == null -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text("片库加载失败，请稍后重试", color = OtvColors.White60, fontSize = 26f.sxs(s))
            }
            else -> {
                // 竖屏同类海报墙统一固定 3 列；横屏继续按可用宽度动态计算。
                val libCols = if (portrait) 3 else com.qiubo.optimaltv.ui.theme.rememberRowColumns(
                    cardW = 232f, gap = 20f, sidePad = 80f,
                )
                val libGap = if (portrait) 12f else 20f
                // 片库状态提升到组合作用域（LazyColumn builder 非组合上下文）
                val categories = remember(cat) { cat.categories.map { it.id to if (it.id.removePrefix("douban:") == "anime") "国漫" else it.name }.sortedBy { listOf("movie", "tv", "anime", "variety", "short").indexOf(it.first.removePrefix("douban:")).let { rank -> if (rank < 0) Int.MAX_VALUE else rank } } + ("" to "全部 ›") }
                // v1.17 需求：进页恢复上次选中的分类 chip（按分类 ID 持久化），不回「电影」
                var catSel by remember(cat) { mutableStateOf(0) }
                LaunchedEffect(cat) {
                    val saved = runCatching { Graph.settings.vodLastCat() }.getOrNull()
                    if (!saved.isNullOrBlank()) {
                        categories.indexOfFirst { it.first == saved }.takeIf { it >= 0 }?.let { catSel = it }
                    }
                }
                // 分类 tab 下三块（原版 LIB_BLOCKS 数据源：最近热门=home 板块 / 最新上线=channel / 最近更新=latest；
                // home/channel/latest 全挂时降级频道页三栏目 channel-sections）。「全部 ›」独立跳转 all 页
                val selCatId = categories.getOrNull(catSel)?.first.orEmpty()
                var sectionLoading by remember(selCatId) { mutableStateOf(true) }
                LaunchedEffect(selCatId) {
                    try { Graph.repo.channelSections(selCatId) } finally { sectionLoading = false }
                }
                /* 分类 id 现为 `douban:movie` 形态；用与 libBlocks 同一套解析，
                   否则恒为 null → 三栏恒空（影视 tab 一直「暂无内容」）。 */
                val selCid = com.qiubo.optimaltv.data.repo.VodRepository.categoryCid(selCatId)
                /* 需求④（与 TV 版同款）：热门块就绪前不渲染部分板块/兜底网格，
                 * 防「兜底网格→部分板块→热门插最前」结构顶替导致的列表跳动 */
                val doubanSections by Graph.repo.sectionsFlow.collectAsStateWithLifecycle()
                val sections = if (selCid == null) emptyList() else doubanSections[selCid].orEmpty()

                val showLibLoading = sectionLoading && sections.isEmpty()
                val sectionPages = remember(sections) {
                    MOBILE_LIBRARY_SECTION_TITLES.mapIndexed { index, title ->
                        title to MOBILE_LIBRARY_SECTION_SOURCE_ALIASES[index]
                            .firstNotNullOfOrNull { alias -> sections.firstOrNull { it.name == alias }?.items }
                            .orEmpty()
                    }
                }
                val libraryPagerState = rememberPagerState(pageCount = { MOBILE_LIBRARY_SECTION_TITLES.size })
                val libraryRowStates = List(MOBILE_LIBRARY_SECTION_TITLES.size) {
                    List(2) { rememberLazyListState() }
                }
                LaunchedEffect(selCid) {
                    libraryPagerState.scrollToPage(0)
                    libraryRowStates.forEach { rows -> rows.forEach { it.scrollToItem(0) } }
                }

                LazyColumn(
                    state = vodListState,
                    modifier = Modifier
                        .fillMaxSize()
                        .background(OtvColors.Bg),
                ) {
                    item(key = "hero") {
                        VodHeroBand(nav, heroesTop, s, portrait, history.map { it.vodId }.toSet())
                    }
                    // 最近观看（需求 影视#7：竖版卡=片库同款 232×352，片名在卡下；需求 影视#10 点卡直进播放）
                    item(key = "history") {
                        Column(Modifier.padding(top = 24f.sx(s))) {
                            Text(
                                "最近观看", color = OtvColors.White, fontSize = 34f.sxs(s), fontWeight = FontWeight.Bold,
                                modifier = Modifier.padding(start = homePad),
                            )
                            Spacer(Modifier.height(20f.sx(s)))
                            if (history.isEmpty()) {
                                Text(
                                    "最近还没有观看记录", color = OtvColors.White50, fontSize = 26f.sxs(s),
                                    modifier = Modifier.padding(start = homePad),
                                )
                            } else if (portrait) {
                                Column(
                                    verticalArrangement = Arrangement.spacedBy(24f.sx(s)),
                                    modifier = Modifier.padding(horizontal = homePad),
                                ) {
                                    history.chunked(3).forEach { historyRow ->
                                        Row(
                                            horizontalArrangement = Arrangement.spacedBy(12f.sx(s)),
                                            modifier = Modifier.fillMaxWidth(),
                                        ) {
                                            historyRow.forEach { h ->
                                                HistoryCard(nav, h, s, Modifier.weight(1f), compact = true)
                                            }
                                            repeat(3 - historyRow.size) { Spacer(Modifier.weight(1f)) }
                                        }
                                    }
                                }
                            } else {
                                LazyRow(
                                    horizontalArrangement = Arrangement.spacedBy(20f.sx(s)),
                                    contentPadding = PaddingValues(horizontal = homePad),
                                ) {
                                    items(history.size, key = { i -> history[i].vodId }) { i ->
                                        HistoryCard(nav, history[i], s, Modifier.width(232f.sx(s)))
                                    }
                                }
                            }
                        }
                    }
                    // 片库三栏目纵向分页；每页两条同步横向浏览的海报行，与 TV 版顺序一致。
                    item(key = "lib-chips") {
                        VodCategorySelector(
                            categories = categories,
                            selectedId = selCatId,
                            portrait = portrait,
                            s = s,
                            sidePadding = homePad,
                            onSelect = { id ->
                                catSel = categories.indexOfFirst { it.first == id }.coerceAtLeast(0)
                                Graph.appScope.launch {
                                    runCatching { Graph.settings.rememberVodLastCat(id) }
                                }
                            },
                            onAll = { nav.navigate("all") },
                        )
                    }
                    item(key = "lib-vertical-three-pages") {
                        androidx.compose.foundation.layout.BoxWithConstraints(Modifier.fillMaxWidth()) {
                            val cardWidth = (maxWidth - homePad * 2 - libGap.sx(s) * (libCols - 1)) / libCols
                            val rowHeight = cardWidth * (352f / 232f) + 42f.sx(s)
                            val pagerHeight = rowHeight * 2 + 120f.sx(s)
                            VerticalPager(
                                state = libraryPagerState,
                                beyondBoundsPageCount = 1,
                                userScrollEnabled = true,
                                modifier = Modifier.fillMaxWidth().height(pagerHeight),
                                key = { page -> "vod-library-page-${MOBILE_LIBRARY_SECTION_TITLES[page]}" },
                                flingBehavior = PagerDefaults.flingBehavior(
                                    state = libraryPagerState,
                                    pagerSnapDistance = PagerSnapDistance.atMost(1),
                                    snapAnimationSpec = spring(
                                        dampingRatio = Spring.DampingRatioNoBouncy,
                                        stiffness = Spring.StiffnessMediumLow,
                                    ),
                                ),
                            ) { page ->
                                val (title, pageItems) = sectionPages[page]
                                MobileLibrarySectionPage(
                                    nav = nav,
                                    title = title,
                                    items = pageItems,
                                    loading = showLibLoading,
                                    s = s,
                                    compact = portrait,
                                    cols = libCols,
                                    gap = libGap,
                                    sidePadding = homePad,
                                    rowStates = libraryRowStates[page],
                                )
                            }
                        }
                    }
                    item(key = "lib-pad") { Spacer(Modifier.height(80f.sx(s))) }
                }
            }
        }
        // TabBar 悬浮：固定海报直接透出，仅保留顶黑渐变保证可读性（触控版：滚动下潜）
        MainTabBar(
            "vod", { key -> navigateToTab(nav, key) }, { nav.navigate("search") },
            hidden = chromeGone,
        )
        OtvHint(hint)
    }
}

/** hero 条目（三来源归一：carousel 横版大图 / 首页热门板块竖版封面 / 目录竖版封面） */
private data class HeroEntry(val id: String, val title: String, val bg: String, val tag: String)

/** 原版 refreshVod hero 降级链：carousel≥3 → 首页热门板块合并(hotHeroItems) → 目录前 6 */
private fun buildHeroes(cat0: com.qiubo.optimaltv.data.repo.Aggregated?, home: com.qiubo.optimaltv.data.repo.VodRepository.HomeState): List<HeroEntry> {
    if (home.carousel.size >= 3) {
        return home.carousel.map { HeroEntry("hhkan:" + it.vid, it.title, it.backdrop, it.tags.firstOrNull().orEmpty()) }
    }
    /* 2026-09-18：键表与 VodRepository.heroRefs 保持一致（设备端豆瓣 /vod/home 的板块名
       是「最近热门 / 最新上映 / 豆瓣高分」，旧键表只有好好看时代的「近期热门…」）。 */
    val hotKeys = listOf("近期热门", "最近更新", "近期热播", "热播", "近期热门电影", "近期热门剧集",
        "最近热门", "最新上映", "豆瓣高分")
    val hot = home.sections
        .filter { sec -> hotKeys.any { sec.name.contains(it) } }
        .flatMap { sec -> sec.items.map { HeroEntry(it.id, it.title, it.heroUrl.ifBlank { it.posterUrl }, sec.name) } }
        .distinctBy { it.id }
    if (hot.isNotEmpty()) return hot.take(8)
    return cat0?.dedupedItems?.filter { it.posterUrl.isNotBlank() }?.take(6)
        ?.map { HeroEntry(it.id, it.title, it.heroUrl.ifBlank { it.posterUrl }, "") }.orEmpty()
}

/**
 * 固定 hero 海报带（v1.18 与 TV 版 1:1 同款布局，需求 #1/#8/#15/#16）：
 * 海报顶到屏幕最顶（无留白）+ 半透明白雾；带高 VOD_BAND（需求⑤ 竖屏缩短上移），
 * 非全屏背景；触控交互：横滑翻页（TV 版为播放钮上左右键手动切换，同「不自动轮播」语义），
 * 圆点可点直达；vcontent（横屏 top 340 / 竖屏 top 170：title 44 / cat 27 / desc 两行 / btn 白50% h64）。
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun VodHeroBand(
    nav: NavController,
    heroes: List<HeroEntry>,
    s: Float,
    portrait: Boolean,
    /** 有观看记录的影片 id：hero 主按钮据此显示「继续播放」（用户 2026-09-17 要求） */
    watchedIds: Set<String>,
) {
    val heroCtx = LocalContext.current
    val heroScope = androidx.compose.runtime.rememberCoroutineScope()
    val pagerState = rememberPagerState(initialPage = 0) { heroes.size.coerceAtLeast(1) }
    val bandH = if (portrait) VOD_BAND_PORTRAIT else VOD_BAND
    Box(Modifier.fillMaxWidth().height(bandH.sx(s))) {
        if (heroes.isEmpty()) {
            Box(Modifier.fillMaxSize().background(OtvColors.Panel))
        } else {
            HorizontalPager(
                state = pagerState,
                modifier = Modifier.fillMaxSize(),
            ) { page ->
                val h = heroes.getOrNull(page) ?: return@HorizontalPager
                Box(Modifier.fillMaxSize()) {
                    AsyncImage(
                        // v1.10：ImageLoader 已放开硬件位图（低端机滚动性能），仅 hero 超宽图
                        //（3360×1080，硬件位图解码失败=图空白）按请求走软件解码
                        model = remember(heroCtx, h.bg) {
                            coil.request.ImageRequest.Builder(heroCtx)
                                .data(h.bg)
                                .allowHardware(false)
                                .build()
                        },
                        contentDescription = h.title,
                        contentScale = ContentScale.Crop,
                        modifier = Modifier.fillMaxSize(),
                    )
                    // 半透明白雾（需求 #1 透明度）：海报显通透且文字可读
                    Box(Modifier.fillMaxSize().background(Color.White.copy(alpha = 0.10f)))
                    // grad-l（90deg .22 0% / .08 30% / 0 55%）
                    Box(
                        Modifier
                            .fillMaxSize()
                            .background(
                                Brush.horizontalGradient(
                                    0f to Color(0x38000000),
                                    0.30f to Color(0x14000000),
                                    0.55f to Color.Transparent,
                                ),
                            ),
                    )
                    // 底部渐黑：与下方内容区（实底 Bg）自然衔接
                    Box(
                        Modifier
                            .align(Alignment.BottomCenter)
                            .fillMaxWidth()
                            .height(160f.sx(s))
                            .background(Brush.verticalGradient(0f to Color.Transparent, 1f to OtvColors.Bg)),
                    )
                    // vcontent（横屏 top 340 / 竖屏 top 170；竖屏宽 640 防溢出窄屏）
                    // 2026-09-19 回滚「整体上移 340→250 / 170→120」：那次上移是对
                    // 「简介把播放钮顶出海报带」的位移规避，现已按根因修（文字列封顶 + 钮固定到
                    // 带底设计位），位置恢复设计稿，与 TV 版重新 1:1 对齐。
                    Column(
                        Modifier
                            .align(Alignment.TopStart)
                            .padding(
                                start = if (portrait) 40f.sx(s) else 91f.sx(s),
                                top = (if (portrait) 170f else 340f).sx(s),
                            )
                            .width((if (portrait) 640f else 760f).sx(s))
                            // 文字列总闸（设计内容高 210）：单块见各自 heightIn
                            .heightIn(max = VOD_HERO_TEXT_MAX.sx(s)),
                    ) {
                        Text(
                            h.title, color = OtvColors.White, fontSize = 44f.sxs(s), fontWeight = FontWeight.Bold,
                            lineHeight = 46f.sxs(s), maxLines = 1, overflow = TextOverflow.Ellipsis,
                            // 行高/字体内边距随设备字体变的夹子（设计框高 64）
                            modifier = Modifier.heightIn(max = VOD_HERO_TITLE_MAX.sx(s)),
                            style = androidx.compose.ui.text.TextStyle(
                                shadow = androidx.compose.ui.graphics.Shadow(
                                    Color(0x99000000),
                                    offset = androidx.compose.ui.geometry.Offset(0f, 2f),
                                    blurRadius = 20f.sx(s) / 1.dp,
                                ),
                            ),
                        )
                        Spacer(Modifier.height(8f.sx(s)))
                        // vod-cat（meta 行先显 tag，detail 到达后覆盖；评分拼行尾）
                        var metaLine by remember(h.id) { mutableStateOf(if (h.tag.isBlank()) emptyList() else listOf(h.tag)) }
                        var desc by remember(h.id) { mutableStateOf("") }
                        var rating by remember(h.id) { mutableStateOf(0.0) }
                        LaunchedEffect(h.id) {
                            desc = "加载简介…"
                            Graph.repo.vodBrief(h.id)?.let { (m, d, r) ->
                                if (m.isNotBlank()) metaLine = m.split("/").map { it.trim() }.filter { it.isNotBlank() }
                                desc = d.ifBlank { h.title + " · My TV 精选内容，一键播放。" }
                                rating = r
                            } ?: run { desc = h.title + " · My TV 精选内容，一键播放。" }
                        }
                        if (metaLine.isNotEmpty() || rating > 0) {
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(14f.sx(s)),
                                // 夹回设计框高 54（设备字体行高异常时 meta 不再把简介/按钮往下推）
                                modifier = Modifier.heightIn(max = VOD_HERO_META_MAX.sx(s)),
                            ) {
                                if (metaLine.isNotEmpty()) {
                                    Text(
                                        // 2026-09-19：段数上限 6——豆瓣 regions+genres 最多 10 段
                                        //（奥德赛：2026/美国/加拿大/冰岛/新西兰/摩洛哥/英国/动作/历史/奇幻），
                                        // 760 宽单行放不下会被省略号截断且把评分挤出列外
                                        metaLine.take(6).joinToString(" / "),
                                        color = OtvColors.White.copy(alpha = 0.8f), fontSize = 27f.sxs(s), fontWeight = FontWeight.Medium,
                                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                                    )
                                }
                                if (rating > 0) {
                                    Text("★ " + "%.1f".format(rating), color = OtvColors.White50, fontSize = 15f.sxs(s), fontWeight = FontWeight.Bold)
                                }
                            }
                        }
                        if (desc.isNotBlank()) {
                            Spacer(Modifier.height(10f.sx(s)))
                            Text(
                                desc, color = OtvColors.White55, fontSize = 27f.sxs(s), fontWeight = FontWeight.Medium,
                                lineHeight = 35f.sxs(s), maxLines = 2, overflow = TextOverflow.Ellipsis,
                                // 夹回设计框高 74（两行）；超出只收敛行数，不再挤压按钮
                                modifier = Modifier.heightIn(max = VOD_HERO_DESC_MAX.sx(s)),
                            )
                        }
                    }
                    // vod-actions：btn-vod 白50%底黑字 h64 px40 r32 字26 Bold
                    // 2026-09-19 修复：钮从文字列尾部移到海报带底固定位（设计位），
                    // 与 TV 版同一口径；文字列再长也只会让 desc 收敛行数（各块 heightIn 封顶），
                    // 钮不再被顶出带外（用户报障：奥德赛钮整颗消失）。
                    Box(
                        contentAlignment = Alignment.Center,
                        modifier = Modifier
                            .align(Alignment.BottomStart)
                            .padding(
                                start = if (portrait) 40f.sx(s) else 91f.sx(s),
                                bottom = (if (portrait) VOD_HERO_BTN_BOTTOM_PORT else VOD_HERO_BTN_BOTTOM_LAND).sx(s),
                            )
                            .clip(RoundedCornerShape(32f.sx(s)))
                            .tapCard { nav.navigate("detail/" + URLEncoder.encode(h.id, "UTF-8")) }
                            .background(OtvColors.White.copy(alpha = 0.5f), RoundedCornerShape(32f.sx(s)))
                            .height(64f.sx(s))
                            .padding(horizontal = 40f.sx(s)),
                    ) {
                        Text(
                            if (h.id in watchedIds) "继续播放" else "立即播放",
                            color = OtvColors.Bg, fontSize = 26f.sxs(s), fontWeight = FontWeight.Bold,
                        )
                    }
                }
            }
            // carousel-dots（带内底部居中：黑50%胶囊，当前页白点；触控可点直达）
            if (heroes.size > 1) {
                Row(
                    horizontalArrangement = Arrangement.spacedBy(8f.sx(s)),
                    modifier = Modifier
                        .align(Alignment.BottomCenter)
                        .padding(bottom = 30f.sx(s))
                        .background(Color(0x80000000), RoundedCornerShape(28f.sx(s)))
                        .padding(horizontal = 10f.sx(s), vertical = 6f.sx(s)),
                ) {
                    heroes.forEachIndexed { i, _ ->
                        Box(
                            Modifier
                                .clip(CircleShape)
                                .tapCard { heroScope.launch { pagerState.animateScrollToPage(i) } }
                                .padding(3f.sx(s))
                                .size(10f.sx(s))
                                .background(
                                    if (i == pagerState.currentPage) OtvColors.White else OtvColors.White30,
                                    CircleShape,
                                ),
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun MobileLibrarySectionPage(
    nav: NavController,
    title: String,
    items: List<VodItem>,
    loading: Boolean,
    s: Float,
    compact: Boolean,
    cols: Int,
    gap: Float,
    sidePadding: androidx.compose.ui.unit.Dp,
    rowStates: List<androidx.compose.foundation.lazy.LazyListState>,
) {
    val rows = remember(items) {
        listOf(
            items.filterIndexed { index, _ -> index % 2 == 0 },
            items.filterIndexed { index, _ -> index % 2 == 1 },
        )
    }
    val alignedColumnCount = rows.maxOfOrNull { it.size } ?: 0
    androidx.compose.foundation.layout.BoxWithConstraints(Modifier.fillMaxSize()) {
        val cardWidth = (maxWidth - sidePadding * 2 - gap.sx(s) * (cols - 1)) / cols
        val rowHeight = cardWidth * (352f / 232f) + 42f.sx(s)
        val rowItemPitchPx = with(LocalDensity.current) { (cardWidth + gap.sx(s)).toPx() }
        LaunchedEffect(rowStates, alignedColumnCount, rowItemPitchPx) {
            fun positions() = rowStates.map { state ->
                state.firstVisibleItemIndex * rowItemPitchPx + state.firstVisibleItemScrollOffset
            }
            var lastPositions = positions()
            snapshotFlow { positions() }.collect { currentPositions ->
                val changedRows = currentPositions.indices.filter { currentPositions[it] != lastPositions[it] }
                if (changedRows.size == 1) {
                    val sourceRow = changedRows.single()
                    val targetRow = 1 - sourceRow
                    val delta = currentPositions[sourceRow] - lastPositions[sourceRow]
                    if (delta != 0f && alignedColumnCount > 1) {
                        rowStates[targetRow].scrollBy(delta)
                        lastPositions = positions()
                    } else {
                        lastPositions = currentPositions
                    }
                } else {
                    lastPositions = currentPositions
                }
            }
        }
        Column(
            modifier = Modifier
                .fillMaxSize()
                .background(OtvColors.Bg)
                .padding(horizontal = sidePadding, vertical = 16f.sx(s)),
            verticalArrangement = Arrangement.spacedBy(18f.sx(s), Alignment.CenterVertically),
        ) {
            Text(title, color = OtvColors.White.copy(alpha = 0.92f), fontSize = 34f.sxs(s), fontWeight = FontWeight.Bold)
            if (items.isEmpty()) {
                Box(Modifier.fillMaxWidth().weight(1f), contentAlignment = Alignment.Center) {
                    Text(if (loading) "正在加载…" else "暂无${title}内容", color = OtvColors.White50, fontSize = 26f.sxs(s))
                }
            } else {
                repeat(2) { row ->
                    val rowItems = rows[row]
                    LazyRow(
                        state = rowStates[row],
                        modifier = Modifier.fillMaxWidth().height(rowHeight),
                        horizontalArrangement = Arrangement.spacedBy(gap.sx(s)),
                    ) {
                        items(
                            count = alignedColumnCount,
                            key = { column -> rowItems.getOrNull(column)?.id ?: "lib:$title:alignment-spacer:$row:$column" },
                        ) { column ->
                            val item = rowItems.getOrNull(column)
                            if (item == null) {
                                Spacer(Modifier.width(cardWidth).height(rowHeight))
                            } else {
                                MobileVodCard(
                                    item = item,
                                    s = s,
                                    compact = compact,
                                    modifier = Modifier.width(cardWidth),
                                    onClick = { nav.navigate("detail/" + URLEncoder.encode(item.id, "UTF-8")) },
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}

/**
 * 最近观看卡（需求 影视#7：竖版 232×352 与片库同款，片名放卡片下方；
 * 「上次看到 第N集 时间」在封面右下角灰底角标；最底细进度条）。
 * 需求 影视#10：点卡直接进播放页（带历史集数）。
 */
@Composable
private fun HistoryCard(
    nav: NavController,
    h: HistoryEntity,
    s: Float,
    modifier: Modifier,
    compact: Boolean = false,
) {
    // 旧版本曾以空标题入库（目录外条目详情未回填片名）：空标题时懒解析详情自愈
    var title by remember(h.vodId) { mutableStateOf(h.title) }
    LaunchedEffect(h.vodId) {
        if (title.isBlank()) Graph.repo.resolveDetail(h.vodId)?.let { if (it.title.isNotBlank()) title = it.title }
    }
    Column(modifier) {
        Box(
            Modifier
                .clip(RoundedCornerShape(12f.sx(s)))
                .tapCard { nav.navigate("player/" + URLEncoder.encode(h.vodId, "UTF-8") + "/${h.epIndex}") }
                .fillMaxWidth()
                .aspectRatio(232f / 352f),
        ) {
            PosterPlaceholder(seed = h.vodId.hashCode(), modifier = Modifier.fillMaxSize())
            if (h.posterUrl.isNotBlank()) {
                AsyncImage(model = h.posterUrl, contentDescription = title, contentScale = ContentScale.Crop, modifier = Modifier.fillMaxSize())
            }
            // v1.17 需求：「上次看到 第N集 时间」移入封面右下角 + 灰色背景（替代卡下方副标题）
            Text(
                "上次看到 第${h.epIndex + 1}集 " + formatTime(h.positionMs),
                color = OtvColors.White,
                fontSize = (if (compact) 13f else 17f).sxs(s),
                lineHeight = (if (compact) 16f else 20f).sxs(s),
                maxLines = 1,
                modifier = Modifier
                    .align(Alignment.BottomEnd)
                    .padding(6f.sx(s))
                    .background(Color(0x99666668), RoundedCornerShape(5f.sx(s)))
                    .padding(horizontal = 8f.sx(s), vertical = 3f.sx(s)),
            )
            // 最底细进度条：观看进度（positionMs/durationMs，旧记录 duration=0 时不显示满条）
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
            fontSize = (if (compact) 20f else 24f).sxs(s),
            fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis,
        )
    }
}

/** 观看时间文案（需求 影视#2：去掉「今天」前缀）：今天=HH:mm，昨天=昨天 HH:mm，更早=MM-dd HH:mm */
fun historyTimeLabel(ts: Long): String {
    if (ts <= 0) return ""
    val dayFmt = java.text.SimpleDateFormat("yyyyMMdd", java.util.Locale.CHINA)
    val now = java.util.Calendar.getInstance()
    return when (dayFmt.format(java.util.Date(ts))) {
        dayFmt.format(now.time) ->
            java.text.SimpleDateFormat("HH:mm", java.util.Locale.CHINA).format(java.util.Date(ts))
        dayFmt.format(java.util.Date(now.timeInMillis - 86_400_000L)) ->
            "昨天 " + java.text.SimpleDateFormat("HH:mm", java.util.Locale.CHINA).format(java.util.Date(ts))
        else -> java.text.SimpleDateFormat("MM-dd HH:mm", java.util.Locale.CHINA).format(java.util.Date(ts))
    }
}

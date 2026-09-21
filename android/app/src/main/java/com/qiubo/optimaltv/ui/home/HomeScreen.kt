package com.qiubo.optimaltv.ui.home

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.background
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavController
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.db.HistoryEntity
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.data.model.formatTime
import com.qiubo.optimaltv.data.source.HhkanSource
import com.qiubo.optimaltv.ui.components.FocusRegistry
import com.qiubo.optimaltv.ui.components.InitialFocusEffect
import com.qiubo.optimaltv.ui.components.MainTabBar
import com.qiubo.optimaltv.ui.components.NAV_DOWN
import com.qiubo.optimaltv.ui.components.NAV_LEFT
import com.qiubo.optimaltv.ui.components.NAV_RIGHT
import com.qiubo.optimaltv.ui.components.NAV_UP
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.PosterPlaceholder
import com.qiubo.optimaltv.ui.components.ProvideNavScrolls
import com.qiubo.optimaltv.ui.components.ReturnFocus
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.components.lazyNavContainer
import com.qiubo.optimaltv.ui.components.navigateToTab
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sxs
import androidx.compose.foundation.layout.size
import com.qiubo.optimaltv.ui.theme.sx
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.net.URLEncoder

/**
 * 影视页（原版 view-vod，2026-08-28 重排）：
 * 整页单 LazyColumn（需求 影视#1/#5：hero 海报带随内容滚动，下方最近观看/片库可上下滚动）——
 * 首项 = hero 海报带（VOD_BAND 高：海报顶到屏幕最顶不留白 + 半透明白雾）；
 * 之后 = 最近观看 + 片库（分类 chips + 网格）。
 * hero 左右键手动切换（播放钮上），不自动轮播（需求 #15）。
 */
private const val VOD_BAND = 700f

/**
 * hero 各文字块的高度上限（设计值 + 4px 余量）：title 64 / meta 54 / desc 74。
 * 2026-09-19 修复（用户报障「奥德赛」立刻播放钮整颗消失）：截图实测设备上 title/meta
 * 的文字框比设计高约 90px（字体行高差异），三行累计把尾部按钮顶出海报带、meta 与简介
 * 也整体下移。逐块封顶后，异常设备的行高被夹回设计值——位置与正常设备逐像素一致。
 */
private const val VOD_HERO_TITLE_MAX = 68f
private const val VOD_HERO_META_MAX = 58f
private const val VOD_HERO_DESC_MAX = 78f

/** hero 文字列总上限（= 68 + 8 + 58 + 10 + 78 = 222），总闸，兜住任何单块漏网 */
private const val VOD_HERO_TEXT_MAX = 222f

/** hero 播放钮距海报带底部（设计钮框 y 576..640，带高 700 → 底部留 60） */
private const val VOD_HERO_BTN_BOTTOM = 60f

@Composable
fun VodScreen(nav: NavController) {
    val catalogState by Graph.repo.state.collectAsStateWithLifecycle()
    val history by Graph.db.vodDao().historyFlow(12).collectAsStateWithLifecycle(initialValue = emptyList())
    val s = rememberUiScale()
    // 返回落焦（需求 影视#6-2）：从详情/播放返回时回到上次聚焦的卡片
    val returnKey = remember { ReturnFocus.take("vod") }
    val tabFocus = remember { FocusRequester() }
    val heroPlayFocus = remember { FocusRequester() }
    // 光标导航#3（2026-09-03）：hero 按钮.DOWN 定向目标——最近观看首卡（无历史时回分类 chip）
    val firstHistoryFocus = remember { FocusRequester() }
    val cardReg = remember { FocusRegistry() }
    InitialFocusEffect(tabFocus, "vod-tab", enabled = returnKey == null)
    var restoredFocus by remember { mutableStateOf(false) }
    LaunchedEffect(catalogState.catalog, history) {
        val key = returnKey ?: return@LaunchedEffect
        if (restoredFocus) return@LaunchedEffect
        if (catalogState.catalog == null && history.isEmpty()) return@LaunchedEffect
        restoredFocus = true
        // "hero" 用播放钮自带的 heroBtnFocus（registry 里没有注册该 key）
        val target = if (key == "hero") heroPlayFocus else cardReg.fr(key)
        repeat(40) {
            if (runCatching { target.requestFocus() }.isSuccess) return@LaunchedEffect
            delay(100)
        }
        runCatching { tabFocus.requestFocus() }
    }

    // hero 数据（原版 refreshVod 降级链：源站轮播 carousel≥3 优先 → 首页热门板块合并 → 目录前 6 兜底）
    val home by Graph.repo.homeFlow.collectAsStateWithLifecycle()
    /* 2026-09-18：进页兜底——目录为空（后端冷启动/热更重启期首轮抓取全失败）时补抓，
       否则 hero 会退化成空底板、分类卡与三栏全空。 */
    LaunchedEffect(Unit) {
        Graph.repo.ensureCatalogFresh(force = true)
        Graph.repo.refreshHome(force = true)
    }
    val cat0 = catalogState.catalog
    val heroesTop = remember(cat0, home) { buildHeroes(cat0, home) }
    var heroIdxTop by remember(heroesTop) { mutableIntStateOf(0) }
    // 需求 #15：海报不自动轮播，仅焦点在播放钮上左右键手动切换

    // 需求 #2：任意处按返回 → 回顶部（滚动归零 + 焦点回当前 tab）；
    // 已在顶部且焦点在 tab 栏 → 双击才彻底退出应用（需求⑥，首按轻提示）
    val vodListState = rememberLazyListState()
    val backScope = androidx.compose.runtime.rememberCoroutineScope()
    var tabFocused by remember { mutableStateOf(false) }
    var hint by remember { mutableStateOf<String?>(null) }
    // 需求②：分类 chip 行「从任何地方进入都回上次选中项」的行内持焦守卫——
    // gen 代际计数消除 LOST/GAINED 乱序歧义（行内移动时旧 chip 的 LOST 先到，
    // 150ms 内无本行新事件才认定焦点真离开本行）。
    var chipRowHasFocus by remember { mutableStateOf(false) }
    var chipRowGen by remember { mutableIntStateOf(0) }
    // 2026-09-19 跳行修复：分类 chip DOWN 挂起落点（板块未就绪时消费下键+记账，到账定向落首卡）
    var pendingLibDown by remember { mutableStateOf(false) }
    var pendingLibDownAt by remember { mutableStateOf(0L) }
    val chipScope = androidx.compose.runtime.rememberCoroutineScope()
    LaunchedEffect(hint) { if (hint != null) { kotlinx.coroutines.delay(2500); hint = null } }
    val exitApp = com.qiubo.optimaltv.ui.components.rememberDoubleBackExit { hint = it }
    // 2026-09-05（用户反馈）：内容顶行 UP 直达顶部 tab 栏——此前 hero「立即播放」是
    // 唯一入口（几何搜索总会先命中它，tab 栏节点是 floating 不参与垂直搜索），
    // 必须先移到播放钮才能进标签栏。现在最近观看首行 /（无历史时的）分类 chips 在
    // 页面滚到顶时 UP 定向回 tab；未到顶仍走引擎滚动找回（露出 hero）。
    // hero 播放钮不受影响：tab DOWN 仍先落播放钮。
    // 判顶用 firstVisibleItemIndex+offset 容差而非 canScrollBackward：引擎 ensureVisible
    // 落焦卡片时会补滚 ~16px 呼吸边距，canScrollBackward 因此变 true 误判「未到顶」；
    // 命中时顺手把滚动归零清掉残留。
    val contentUpToTab: () -> Boolean = {
        if (vodListState.firstVisibleItemIndex == 0 && vodListState.firstVisibleItemScrollOffset <= 64) {
            backScope.launch {
                runCatching { vodListState.scrollToItem(0) }
                repeat(10) {
                    if (runCatching { tabFocus.requestFocus() }.isSuccess) return@launch
                    delay(50)
                }
            }
            true
        } else false
    }
    BackHandler {
        val atTop = vodListState.firstVisibleItemIndex == 0 && vodListState.firstVisibleItemScrollOffset == 0
        if (atTop && tabFocused) {
            exitApp()
        } else {
            backScope.launch { vodListState.scrollToItem(0) }
            runCatching { tabFocus.requestFocus() }
        }
    }

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        val cat = catalogState.catalog
        when {
            catalogState.loading && cat == null -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                com.qiubo.optimaltv.ui.components.AppleLoading()
            }
            cat == null -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text("片库加载失败，请稍后重试", color = OtvColors.White60, fontSize = 26f.sxs(s))
            }
            else -> {
                // 片库状态提升到组合作用域（LazyColumn builder 非组合上下文，remember/LaunchedEffect 不可写在其内）
                val categories = remember(cat) { cat.categories.map { it.id to it.name } + ("" to "全部 ›") }
                // v1.17 需求：进页恢复上次选中的分类 chip（按分类 ID 持久化），不回「电影」
                var catSel by remember(cat) { mutableIntStateOf(0) }
                val selectedCatChipFocus = remember { FocusRequester() }
                LaunchedEffect(cat) {
                    val saved = runCatching { Graph.settings.vodLastCat() }.getOrNull()
                    if (!saved.isNullOrBlank()) {
                        categories.indexOfFirst { it.first == saved }.takeIf { it >= 0 }?.let { catSel = it }
                    }
                }
                // 分类 tab 固定三块，以好好看频道页的当前分类三栏目为主；「全部 ›」独立跳转 all 页。
                val selCatId = categories.getOrNull(catSel)?.first.orEmpty()
                LaunchedEffect(selCatId) { Graph.repo.libBlocks(selCatId, force = true) }
                val libBlocks by Graph.repo.libBlocksFlow.collectAsStateWithLifecycle()
                val hotSettled by Graph.repo.hotSettledFlow.collectAsStateWithLifecycle()
                /* 分类 id 现为 `douban:movie` 形态；用与 libBlocks 同一套解析，
                   否则恒为 null → 三栏恒空（影视 tab 一直「暂无内容」）。 */
                val selCid = com.qiubo.optimaltv.data.repo.VodRepository.categoryCid(selCatId)
                /* 需求④（光标跳行修复）：三栏就绪前不渲染任何板块——
                 * 否则「空态 → 部分板块 → 热门插最前」两次结构顶替会把焦点
                 * 甩到「最新上线」（用户看到按一下下键就滚过最近热门）。 */
                val sections = if (selCid == null) emptyList()
                    else if (selCid !in hotSettled) emptyList()
                    else libBlocks[selCid].orEmpty().ifEmpty {
                        HhkanSource.LIB_BLOCK_ORDER.map { HhkanSource.ChannelSection(it, emptyList()) }
                    }
                val showLibLoading = selCid != null && selCid !in hotSettled && sections.isEmpty()
                // 2026-09-19 跳行修复（用户报障「电视剧→动漫后往下光标乱跑」）：
                // 分类 chip 的 DOWN 不再交给引擎几何+滚动找回——
                // ① 切分类后板块未就绪时 chips 下方无可聚焦目标，滚动找回会把整页下滚、
                //    chip 离场销毁，焦点靠陈旧锚点随机自愈（跳行/光标消失的根源）；
                // ② 已就绪时「lib-sec-板块名-rN」key 跨分类同名，卡片节点原位复用、
                //    位置不变不重新上报 → stamp 冻结在上次切换前，scroll-reveal 的
                //    时间戳守卫（t.stamp >= stepStamp）把活着的合法首行卡误判为尸体，
                //    连滚 3 步超调 2~3 行才落点（实测日志：单次 DOWN 滚 ~1300px）。
                // 改为一律显式定向：有内容 → 直接落首个非空板块首卡；无内容 → 消费
                // 按键并挂起，板块到账后由下述 effect 定向落下。
                val libFirstCard = sections.firstOrNull { it.items.isNotEmpty() }?.items?.firstOrNull()
                /** chip DOWN 定向：滚到首板块标题（首行随之入视口）→ 落首卡（重试覆盖组合时延） */
                fun dropToLibFirstCard() {
                    val first = libFirstCard ?: return
                    chipScope.launch {
                        runCatching { vodListState.scrollToItem(3) }   // 0 hero / 1 history / 2 chips / 3 首板块标题
                        repeat(20) {
                            if (runCatching { cardReg.fr(first.id).requestFocus() }.isSuccess) return@launch
                            delay(60)
                        }
                    }
                }
                // 板块未就绪时 chip DOWN 被消费并挂起；到账后定向落下（焦点仍在分类行 + 10s 内才落）
                LaunchedEffect(libFirstCard, pendingLibDown) {
                    if (!pendingLibDown) return@LaunchedEffect
                    if (libFirstCard == null) return@LaunchedEffect
                    pendingLibDown = false
                    if (!chipRowHasFocus) return@LaunchedEffect
                    if (android.os.SystemClock.uptimeMillis() - pendingLibDownAt > 10_000L) return@LaunchedEffect
                    dropToLibFirstCard()
                }
                // 整页单 LazyColumn：hero 海报带（随内容滚动）→ 最近观看 → 片库。
                // v1.13：方向导航统一 OtvNav 引擎——容器仅声明滚动挂靠（未组合行的滚动找回）
                val vodScroll = remember(vodListState) { lazyNavContainer(vodListState) }
                ProvideNavScrolls(vertical = vodScroll) {
                LazyColumn(
                    state = vodListState,
                    modifier = Modifier
                        .fillMaxSize()
                        .background(OtvColors.Bg),
                ) {
                    item(key = "hero") {
                        VodHeroBand(
                            nav, heroesTop, s, heroIdxTop, heroPlayFocus, tabFocus,
                            selectedCatChipFocus = selectedCatChipFocus,
                            firstHistoryFocus = firstHistoryFocus,
                            hasHistory = history.isNotEmpty(),
                            watchedIds = history.map { it.vodId }.toSet(),
                            listState = vodListState,
                            onPrev = { heroIdxTop = (heroIdxTop - 1 + heroesTop.size.coerceAtLeast(1)) % heroesTop.size.coerceAtLeast(1) },
                            onNext = { heroIdxTop = (heroIdxTop + 1) % heroesTop.size.coerceAtLeast(1) },
                        )
                    }
                    // 最近观看（需求 影视#7：竖版卡=片库同款 232×352，片名在卡下；需求 影视#10 点卡直进播放）
                    item(key = "history") {
                        Column(Modifier.padding(top = 24f.sx(s))) {
                            Text(
                                "最近观看", color = OtvColors.White, fontSize = 34f.sxs(s), fontWeight = FontWeight.Bold,
                                modifier = Modifier.padding(start = 86f.sx(s)),
                            )
                            Spacer(Modifier.height(20f.sx(s)))
                            if (history.isEmpty()) {
                                Text(
                                    "最近还没有观看记录", color = OtvColors.White50, fontSize = 26f.sxs(s),
                                    modifier = Modifier.padding(start = 86f.sx(s)),
                                )
                            } else {
                                val histRowState = rememberLazyListState()
                                val histScroll = remember(histRowState) { lazyNavContainer(histRowState) }
                                ProvideNavScrolls(horizontal = histScroll) {
                                    LazyRow(
                                        state = histRowState,
                                        horizontalArrangement = Arrangement.spacedBy(20f.sx(s)),
                                        contentPadding = PaddingValues(horizontal = 86f.sx(s)),
                                    ) {
                                        items(history.size, key = { i -> history[i].vodId }) { i ->
                                            HistoryCard(nav, history[i], s, cardReg,
                                                focusRequester = if (i == 0) firstHistoryFocus else null,
                                                upToTab = contentUpToTab)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    // 片库 lib-section
                    // v1.10（2026-08-31 低端机优化）：原实现把 chips+全部板块塞进单个
                    // LazyColumn item（非 lazy Column），切页/滚动要一次性组合全部卡片
                    // （3 板块 × ~14 卡 + 兜底网格），低端 GPU 上明显卡顿，且巨 item
                    // 重组时焦点节点成批重建 = 光标乱跑的温床。
                    // 现扁平化为逐行 item：视口外行不组合，视觉间距 1:1 保留
                    // （chips top40 / 板块标题 top26/44+bottom18 / 行距34 / 底 padding80）。
                    item(key = "lib-chips") {
                        Column(Modifier.padding(start = 80f.sx(s), end = 80f.sx(s), top = 40f.sx(s))) {
                // 片库分类 chips（原版 .chip h64 px30 r32 字29；「全部›」px34；active 白50%底黑字）
                val chipRowState = rememberLazyListState()
                val chipScroll = remember(chipRowState) { lazyNavContainer(chipRowState) }
                ProvideNavScrolls(horizontal = chipScroll) {
                    LazyRow(
                        state = chipRowState,
                        horizontalArrangement = Arrangement.spacedBy(18f.sx(s)),
                        contentPadding = PaddingValues(end = 40f.sx(s)),
                    ) {
                    items(categories.size) { i ->
                        val (id, name) = categories[i]
                        val active = i == catSel
                        val isAllChip = id.isBlank()
                        var chipFocused by remember { mutableStateOf(false) }
                        // 需求①：光标选中即点击——切换分类 / 进「全部」页（OK 键与 hover 共用）
                        val activateChip: () -> Unit = {
                            if (isAllChip) {
                                nav.navigate("all")
                            } else {
                                catSel = i
                                Graph.appScope.launch {
                                    runCatching { Graph.settings.rememberVodLastCat(id) }
                                }
                            }
                        }
                        Box(
                            contentAlignment = Alignment.Center,
                            modifier = Modifier
                                .then(
                                    if (active) Modifier.focusRequester(selectedCatChipFocus)
                                    else Modifier
                                )
                                .dpadFocusable(
                                    scaleFocused = 1.05f, focusedBg = OtvColors.White,
                                    focusedBgRadius = 32f.sx(s),
                                    onFocusedChange = { gained ->
                                        chipFocused = gained
                                        if (gained) {
                                            chipRowGen++
                                            val fromInside = chipRowHasFocus
                                            chipRowHasFocus = true
                                            if (!fromInside && !active) {
                                                // 需求②：行外进入落在非选中 chip → 重定向回上次选中项，
                                                // 不触发切换（否则几何落焦会误切到落点 chip）
                                                chipScope.launch {
                                                    repeat(20) {
                                                        delay(50)
                                                        if (runCatching { selectedCatChipFocus.requestFocus() }.isSuccess) return@launch
                                                    }
                                                }
                                            } else if (fromInside && !active) {
                                                // 需求①：行内移动 hover-select（原版移动即切语义）。
                                                // v1.20（用户需求 全部#9）：「全部›」不 hover 触发——
                                                // 选中即跳页会打断浏览，必须 OK 手动点击进入
                                                if (!isAllChip) activateChip()
                                            }
                                        } else {
                                            val gen = ++chipRowGen
                                            chipScope.launch {
                                                delay(150)
                                                if (chipRowGen == gen) chipRowHasFocus = false
                                            }
                                        }
                                    },
                                    // hover-select 由 onFocusedChange 按「行内/行外进入」区分处理，
                                    // 不走引擎 autoSelect（其落焦即点击会让行外进入误切分类）
                                    // 2026-09-05：无最近观看时分类行=内容顶行，UP 由 contentUpToTab
                                    // 定向回 tab（有历史时不拦截——几何搜索自然落到最近观看卡片）
                                    navOverride = { dir ->
                                        when {
                                            dir == NAV_UP && history.isEmpty() -> contentUpToTab()
                                            // 2026-09-19 跳行修复：chip DOWN 一律显式定向/挂起
                                            //（根因与判定见上方 libFirstCard 注释），不走引擎几何
                                            dir == NAV_DOWN -> {
                                                if (libFirstCard != null) dropToLibFirstCard()
                                                else {
                                                    pendingLibDownAt = android.os.SystemClock.uptimeMillis()
                                                    pendingLibDown = true
                                                }
                                                true
                                            }
                                            else -> false
                                        }
                                    },
                                    autoSelect = false,
                                ) {
                                    activateChip()
                                }
                                .background(
                                    // 需求 我的#6：聚焦才白底黑字；已选分类不再常显白底（避免焦点
                                    // 在「全部」上时前一个分类看起来像被选中），改用文字提亮示意
                                    if (chipFocused) OtvColors.White else OtvColors.ChipBg,
                                    RoundedCornerShape(32f.sx(s)),
                                )
                                .height(64f.sx(s))
                                .padding(horizontal = if (isAllChip) 34f.sx(s) else 30f.sx(s)),
                        ) {
                            Text(
                                name,
                                color = if (chipFocused) OtvColors.Bg else if (active) OtvColors.White else OtvColors.White.copy(alpha = 0.6f),
                                fontSize = 29f.sxs(s), fontWeight = FontWeight.Medium,
                            )
                        }
                    }
                }
                } // ProvideNavScrolls(chipScroll)
                        } // lib-chips padding
                    }
                if (showLibLoading) {
                    // 需求④：板块数据就绪前的轻量占位（不可聚焦、高度稳定，不参与结构顶替）
                    item(key = "lib-loading") {
                        Text(
                            "— 片库板块加载中 …",
                            color = OtvColors.White50, fontSize = 26f.sxs(s),
                            modifier = Modifier.padding(start = 86f.sx(s), top = 40f.sx(s)),
                        )
                    }
                } else if (sections.isNotEmpty()) {
                    // lib-grid（column gap44 mt26）× lib-block（column gap18）：标题 38 Bold .92白 + wrap 行 34/28
                    // 2026-09-07 需求①（跳行修复）：item key 必须用【板块名】而非板块序号 si——
                    // 三块到货顺序 = 最新上线/最近更新（channel/latest，快）→ 最近热门（hot 被
                    // awaitHome 卡住，最慢），而 blockOrder 把最近热门排在第 0 位：它迟到插入后，
                    // 后续所有板块序号 +1，序号 key 的全部行 item 被 LazyColumn 视为不同项整体重建，
                    // 聚焦卡片节点销毁 → 焦点被 Compose 甩给任意节点（实测闪跳到屏幕顶部旧节点）
                    // 再靠锚点自愈——锚点坐标在新布局里已错位 N 行，落点即「跳行」。
                    // 板块名在 merge() 里按名去重，作 key 天然唯一且与到货顺序无关：
                    // 迟到板块 = 纯插入，已组合行保持身份，聚焦节点存活，光标原地稳定。
                    sections.forEachIndexed { si, sec ->
                        item(key = "lib-sec-h-${sec.name}") {
                            Text(
                                sec.name, color = OtvColors.White.copy(alpha = 0.92f),
                                fontSize = 38f.sxs(s), fontWeight = FontWeight.Bold,
                                modifier = Modifier
                                    .padding(start = 80f.sx(s), end = 80f.sx(s))
                                    .padding(top = (if (si == 0) 26f else 44f).sx(s))
                                    .padding(bottom = 18f.sx(s)),
                            )
                        }
                        if (sec.items.isEmpty()) {
                            item(key = "lib-sec-empty-${sec.name}") {
                                Text(
                                    "暂无${sec.name}内容", color = OtvColors.White50, fontSize = 26f.sxs(s),
                                    modifier = Modifier.padding(start = 80f.sx(s), end = 80f.sx(s)),
                                )
                            }
                        } else {
                            sec.items.chunked(7).forEachIndexed { ri, rowItems ->
                                item(key = "lib-sec-${sec.name}-r$ri") {
                                    Row(
                                        horizontalArrangement = Arrangement.spacedBy(20f.sx(s)),
                                        modifier = Modifier
                                            .padding(start = 80f.sx(s), end = 80f.sx(s))
                                            .padding(top = if (ri == 0) 0f.sx(s) else 34f.sx(s)),
                                    ) {
                                        rowItems.forEach { LibCard(nav, it, s, cardReg) }
                                        repeat(7 - rowItems.size) { Spacer(Modifier.width(232f.sx(s))) }
                                    }
                                }
                            }
                        }
                    }
                } else {
                    item(key = "lib-empty") {
                        Text(
                            "暂无内容", color = OtvColors.White50, fontSize = 26f.sxs(s),
                            modifier = Modifier.padding(start = 80f.sx(s), top = 26f.sx(s)),
                        )
                    }
                }
                item(key = "lib-pad") { Spacer(Modifier.height(80f.sx(s))) }
                } // LazyColumn
                } // ProvideNavScrolls(vodScroll)
            }
        }
        // TabBar 悬浮：固定海报直接透出，仅保留顶黑渐变保证可读性
        MainTabBar(
            "vod", { key -> navigateToTab(nav, key) }, { nav.navigate("search") }, tabFocus,
            contentDownFocus = heroPlayFocus,
            contentDownScrollTop = { vodListState.scrollToItem(0) },
            onTabFocused = { tabFocused = it },
            autoHide = true,   // v1.16：光标下移进内容区隐藏顶栏，回顶部显示
        )
        OtvHint(hint)
    }
}

/** hero 条目（三来源归一：carousel 横版大图 / 首页热门板块竖版封面 / 目录竖版封面） */
private data class HeroEntry(val id: String, val title: String, val bg: String, val tag: String)

/** 原版 refreshVod hero 降级链：carousel≥3 → 首页热门板块合并(hotHeroItems) → 目录前 6 */
// 降级链口径已上提到 VodRepository.heroRefs：UI 与启动门闸 hero 简介预载必须同一套，
// 否则预载取的 id 与页面真正渲染的 hero 对不上（豆瓣源下 carousel 恒空时预载全落空）。
private fun buildHeroes(cat0: com.qiubo.optimaltv.data.repo.Aggregated?, home: com.qiubo.optimaltv.data.repo.VodRepository.HomeState): List<HeroEntry> =
    com.qiubo.optimaltv.data.repo.heroRefs(cat0, home)
        .map { HeroEntry(it.id, it.title, it.bg, it.tag) }

/**
 * 固定 hero 海报带（需求 #1/#8/#15/#16）：
 * 海报顶到屏幕最顶（无留白）+ 半透明白雾（透明度）；带高 VOD_BAND，非全屏背景；
 * 左右键在播放钮上手动切换影片，海报同步切换，不自动轮播。
 */
@Composable
private fun VodHeroBand(
    nav: NavController,
    heroes: List<HeroEntry>,
    s: Float,
    idx: Int,
    playFocus: FocusRequester,
    tabFocus: FocusRequester,
    selectedCatChipFocus: FocusRequester,
    firstHistoryFocus: FocusRequester,
    hasHistory: Boolean,
    /** 有观看记录的影片 id：hero 主按钮据此显示「继续播放」（用户 2026-09-17 要求） */
    watchedIds: Set<String>,
    listState: androidx.compose.foundation.lazy.LazyListState,
    onPrev: () -> Unit,
    onNext: () -> Unit,
) {
    val hero = heroes.getOrNull(idx % heroes.size.coerceAtLeast(1))
    val heroScope = androidx.compose.runtime.rememberCoroutineScope()
    val heroCtx = androidx.compose.ui.platform.LocalContext.current
    Box(Modifier.fillMaxWidth().height(VOD_BAND.sx(s))) {
        hero?.let { h ->
            // 需求⑦（2026-09-07）：左右切换影片时海报交叉淡入（Crossfade 双层叠化），
            // 不再瞬时跳变；内容列随 remember(h.id) 自然重组
            androidx.compose.animation.Crossfade(
                targetState = h.bg,
                animationSpec = androidx.compose.animation.core.tween(durationMillis = 450),
                label = "vodHeroBg",
            ) { url ->
                AsyncImage(
                    // v1.10：ImageLoader 已放开硬件位图（低端机滚动性能），仅 hero 超宽图
                    // （3360×1080，MuMu 硬件位图解码失败=图空白）按请求走软件解码
                    model = coil.request.ImageRequest.Builder(heroCtx)
                        .data(url)
                        .allowHardware(false)
                        .build(),
                    contentDescription = h.title,
                    contentScale = ContentScale.Crop,
                    onState = { st ->
                        if (st is coil.compose.AsyncImagePainter.State.Error) {
                            com.qiubo.optimaltv.OtvLog.w("vod hero poster 加载失败: ${st.result.throwable?.message} url=${h.bg.take(100)}")
                        }
                    },
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
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
        // vcontent（top 340：title 44 / cat 27 / desc 两行；播放钮已移出本列，见下方注释）
        hero?.let { h ->
            Column(
                Modifier
                    .align(Alignment.TopStart)
                    .padding(start = 91f.sx(s), top = 340f.sx(s))
                    .width(760f.sx(s))
                    // 2026-09-19：文字列总闸（设计内容高 210）——单块见各自 heightIn
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
                                // 2026-09-19：段数上限 6——豆瓣 regions+genres 最多可到 10 段
                                //（奥德赛：2026/美国/加拿大/冰岛/新西兰/摩洛哥/英国/动作/历史/奇幻），
                                // 760 宽单行放不下会被省略号截断且把评分挤出列外；截到 6 段与其它影片观感一致
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
            // vod-actions：btn-vod 白50%底黑字 h64 px40 r32 字26 Bold；聚焦纯白（focusedBg）+scale1.02
            // 2026-09-19 修复：钮从文字列尾部移到海报带底固定位（设计钮框 y576..640，距带底 60）。
            // 起因：用户报障「奥德赛」——meta 10 段 + 简介更长时文字列被撑高，钮被顶出海报带、
            // 整颗不可见（截图实测钮落在带外）。现在钮不参与文字流测量，简介再长也只让 desc
            // 收敛行数（VOD_HERO_TEXT_MAX 封顶），钮位置与设计稿逐像素一致。
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier
                    .align(Alignment.BottomStart)
                    .padding(start = 91f.sx(s), bottom = VOD_HERO_BTN_BOTTOM.sx(s))
                    .focusRequester(playFocus)
                    .dpadFocusable(
                        scaleFocused = 1.02f, focusedBg = OtvColors.White, focusedBgRadius = 32f.sx(s),
                        // 需求 #4：焦点在 hero 播放钮上时左右键翻页切换影片，海报跟随切换；
                        // UP 由引擎 topFocus 逃逸回当前页 tab（无需显式声明）
                        navOverride = { dir ->
                            when (dir) {
                                NAV_LEFT -> if (heroes.size > 1) { onPrev(); true } else false
                                NAV_RIGHT -> if (heroes.size > 1) { onNext(); true } else false
                                // v1.17：DOWN 定向到「当前选中分类」chip——几何搜索会落在
                                // 第一个 chip（电影）上，hover-select 立即切回电影并覆盖上次
                                // 分类记忆（实测根因）；定向到选中项后 hover-select 无害。
                                // 光标导航#3（2026-09-03）：有最近观看时 DOWN 先落最近观看
                                // 首卡（用户报障：hero 跳过最近观看直达 chips）；无历史再落 chip。
                                // 目标行可能尚未组合：先滚一步露出，再请求焦点。不能先 requestFocus——
                                // LazyColumn 预组合的零尺寸节点也可能让 requestFocus「成功」，随后
                                // 焦点稳定器会把无效焦点恢复回 hero，用户就必须再按一次 DOWN。
                                NAV_DOWN -> {
                                    val downTarget = if (hasHistory) firstHistoryFocus else selectedCatChipFocus
                                    heroScope.launch {
                                        // 480px 让 352px 设计高的首卡完整进入视口；只滚 300px
                                        // 在较矮的电视窗口仍会留下裁边，焦点节点会被过滤为无效。
                                        runCatching { listState.scrollBy(480f) }
                                        repeat(8) {
                                            delay(60)
                                            if (runCatching { downTarget.requestFocus() }.isSuccess) return@launch
                                            runCatching { listState.scrollBy(300f) }
                                        }
                                    }
                                    true
                                }
                                else -> false
                            }
                        },
                        // 需求 影视#1：光标移到「立即播放」时页面滚动回最上方
                        onFocusedChange = {
                            if (it) {
                                ReturnFocus.mark("vod", "hero")
                                heroScope.launch { listState.animateScrollToItem(0) }
                            }
                        },
                    ) {
                        nav.navigate("detail/" + URLEncoder.encode(h.id, "UTF-8"))
                    }
                    .background(OtvColors.White.copy(alpha = 0.5f), RoundedCornerShape(32f.sx(s)))
                    .height(64f.sx(s))
                    .padding(horizontal = 40f.sx(s)),
            ) {
                /* 看过的影片换成「继续播放」（用户 2026-09-17 要求） */
                Text(
                    if (h.id in watchedIds) "继续播放" else "立即播放",
                    color = OtvColors.Bg, fontSize = 26f.sxs(s), fontWeight = FontWeight.Bold,
                )
            }
        }
        // carousel-dots（带内底部居中：黑50%胶囊，当前页白点）
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
                            .size(10f.sx(s))
                            .background(
                                if (i == idx % heroes.size) OtvColors.White else OtvColors.White30,
                                CircleShape,
                            ),
                    )
                }
            }
        }
    }
}

/** 片库竖卡（poster 232×352 r12 + remark 右下 + vname 24 白；副行已按用户要求去掉）
 *  需求 影视#3：选中环紧贴卡片边缘。需求 影视#11：remark 徽章高度降为 2/3。 */
@Composable
private fun LibCard(nav: NavController, item: VodItem, s: Float, cardReg: FocusRegistry? = null) {
    Column(Modifier.width(232f.sx(s))) {
        Box(
            Modifier
                .dpadFocusable(
                    scaleFocused = 1.05f,
                    onFocusedChange = { if (it) ReturnFocus.mark("vod", item.id) },
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
        Spacer(Modifier.height(12f.sx(s)))
        Text(item.title, color = OtvColors.White, fontSize = 24f.sxs(s), fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
        // 2026-09-17 用户要求：卡片下方不再显示豆瓣评分与年份，只留片名
    }
}

/**
 * 最近观看卡（需求 影视#7：改竖版——与片库「最近热门」同款 232×352 竖卡，片名放卡片下方；
 * 副标题「上次看到 第N集 时间」在片名下；海报底部细进度条保留观看进度）。
 * 需求 影视#10：点卡直接进播放页（带历史集数）；需求 影视#3：选中环紧贴卡片边缘。
 */
@Composable
private fun HistoryCard(nav: NavController, h: HistoryEntity, s: Float, cardReg: FocusRegistry? = null, focusRequester: FocusRequester? = null, upToTab: (() -> Boolean)? = null) {
    // 旧版本曾以空标题入库（目录外条目详情未回填片名）：空标题时懒解析详情自愈
    var title by remember(h.vodId) { mutableStateOf(h.title) }
    LaunchedEffect(h.vodId) {
        if (title.isBlank()) Graph.repo.resolveDetail(h.vodId)?.let { if (it.title.isNotBlank()) title = it.title }
    }
    Column(Modifier.width(232f.sx(s))) {
        Box(
            Modifier
                .then(if (focusRequester != null) Modifier.focusRequester(focusRequester) else Modifier)
                .dpadFocusable(
                    scaleFocused = 1.05f,
                    onFocusedChange = { if (it) ReturnFocus.mark("vod", "h:" + h.vodId) },
                    externalFocusRequester = cardReg?.fr("h:" + h.vodId),
                    navOverride = if (upToTab != null) {
                        { dir -> if (dir == NAV_UP) upToTab.invoke() else false }
                    } else null,
                ) {
                    nav.navigate("player/" + URLEncoder.encode(h.vodId, "UTF-8") + "/${h.epIndex}")
                }
                .fillMaxWidth()
                .aspectRatio(232f / 352f)
                .clip(RoundedCornerShape(12f.sx(s))),
        ) {
            PosterPlaceholder(seed = h.vodId.hashCode(), modifier = Modifier.fillMaxSize())
            if (h.posterUrl.isNotBlank()) {
                AsyncImage(model = h.posterUrl, contentDescription = title, contentScale = ContentScale.Crop, modifier = Modifier.fillMaxSize())
            }
            // v1.17 需求：「上次看到 第N集 时间」移入封面右下角 + 灰色背景（替代卡下方副标题）
            Text(
                "上次看到 第${h.epIndex + 1}集 " + formatTime(h.positionMs),
                color = OtvColors.White, fontSize = 17f.sxs(s), lineHeight = 20f.sxs(s), maxLines = 1,
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
        // 片名放卡片下方（需求 影视#7）；「上次看到」已移入封面右下角灰底角标（v1.17 需求）
        Text(title, color = OtvColors.White, fontSize = 24f.sxs(s), fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
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

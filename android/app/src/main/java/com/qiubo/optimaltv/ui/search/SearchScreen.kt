package com.qiubo.optimaltv.ui.search

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
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
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.navigation.NavController
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.R
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.ui.components.FocusRegistry
import com.qiubo.optimaltv.ui.components.InitialFocusEffect
import com.qiubo.optimaltv.ui.components.LocalNavOverride
import com.qiubo.optimaltv.ui.components.MainTabBar
import com.qiubo.optimaltv.ui.components.NAV_DOWN
import com.qiubo.optimaltv.ui.components.NAV_UP
import com.qiubo.optimaltv.ui.components.OtvNav
import com.qiubo.optimaltv.ui.components.PinyinIme
import com.qiubo.optimaltv.ui.components.PosterPlaceholder
import com.qiubo.optimaltv.ui.components.ProvideNavScrolls
import com.qiubo.optimaltv.ui.components.ReturnFocus
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.components.lazyGridNavContainer
import com.qiubo.optimaltv.ui.components.lazyNavContainer
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.URLEncoder

/**
 * 搜索页（2026-09-11 键盘条一比一复刻 Figma「tvOS 18 UI Kit」node 5-11799 Search Keyboard）：
 * 顶部标签栏（不变）→ tvOS 18 键盘条（放大镜 + Search/输入 76px Bold 白30 占位 + 右侧 Hold to
 * dictate；底部白30 分割线；键行 123 | SPACE | a-z | ⌫）→
 * 拼音候选词一行（LazyRow 可左右滚动）→ 结果分类 tab（全部/电影/电视剧/动漫/短剧）→ 结果网格。
 *
 * 键行规格（1920 设计系，Figma 实测）：123 键 59×38 / SPACE 键 89×38（白50% 胶囊 r7 黑字 20px，
 * 与字母行中线对齐）；字母键 54×64 无缝 54px 键距（48px Medium 白50%）；⌫ 54×64（SF Symbols
 * delete.left 矢量）。图标 3 枚由 Figma 导出路径转 VectorDrawable（ic_tvos_kbd_*）。
 * 123 键复用同规格切换数字行（设计稿仅含字母版式）；原「搜索/清空」键随旧键盘移除——
 * 搜索仍由候选词上屏后自动触发，清空用 ⌫ 逐字回删。
 *
 * 需求⑤ 光标严格相邻：区域间 UP/DOWN 全部走【定向跳转】（本行 onPreviewKeyEvent 拦截，
 * 指定落点 FocusRequester 带重试，无落点也消费封锁）——绝不放行给 Compose 几何搜索；
 * 行内 LEFT/RIGHT 由 focusRow / scrollRowNav 锁定。光标只可能在
 * 标签栏 ↔ 键盘 ↔ 候选词 ↔ 分类 tab ↔ 结果 之间逐区移动。
 *
 * 搜索 = 目录内过滤 + 全站实时搜索（后端带 10 分钟缓存，防抖 300ms；触发规则见下）。
 */
/** 搜索结果分类 tab（需求④：全部/电影/电视剧/动漫/短剧） */
private val RESULT_CATS = listOf("全部", "电影", "电视剧", "动漫", "短剧")

/** 详情 meta/集数 → 分类（源站频道 tag：欧美剧/国产剧等以「剧」结尾的类型 + 综艺/动漫/短剧直配） */
private val TV_TAG_RE = Regex("欧美剧|国产剧|香港剧|台湾剧|韩国剧|日本剧|海外剧|泰剧|美剧|英剧|连续剧|电视剧|网剧")

/** 调试词注入通道（adb 广播 → 搜索页 query，仅 debug 构建有写入方；绕过模拟器丢键问题） */
object SearchDebugBus {
    @Volatile var pendingQuery: String? = null
}

internal fun classifyVod(meta: String, epCount: Int): String = when {
    meta.contains("短剧") -> "短剧"
    meta.contains("动漫") -> "动漫"
    meta.contains("综艺") -> "综艺"
    TV_TAG_RE.containsMatchIn(meta) -> "电视剧"
    epCount > 1 -> "电视剧"
    else -> "电影"
}

@Composable
fun SearchScreen(nav: NavController) {
    var query by rememberSaveable { mutableStateOf("") }
    // 123 键切换的数字布局（tvOS 同款开关；设计稿仅含字母版式，数字行复用同键规格）
    var numeric by rememberSaveable { mutableStateOf(false) }
    // 返回落焦（需求 影视#6-2）：从结果卡进入详情再返回时，回到该结果卡
    val returnKey = remember { ReturnFocus.take("search") }
    val keyboardFocus = remember { FocusRequester() }   // 'a' 键 = 键盘区锚点
    // 需求④：键盘到顶 UP 定向回顶部标签栏「搜索」chip
    val tabFocus = remember { FocusRequester() }
    val cardReg = remember { FocusRegistry() }
    val scope = rememberCoroutineScope()
    InitialFocusEffect(keyboardFocus, "search-key", enabled = returnKey == null)
    var restoredFocus by remember { mutableStateOf(false) }
    val s = rememberUiScale()
    val context = LocalContext.current

    // 区域落点句柄（需求⑤：定向跳转目标）
    val firstCandFocus = remember { FocusRequester() }   // 候选词行首
    val firstTabFocus = remember { FocusRequester() }    // 分类 tab 行首（全部）
    val firstResultFocus = remember { FocusRequester() } // 结果首卡
    // 结果网格到顶 UP（引擎逃逸兜底）回分类 tab 行
    LaunchedEffect(Unit) { OtvNav.topFocus = firstTabFocus }

    /** 定向落焦带重试（目标节点可能尚未组合/布局完成） */
    fun focusTarget(fr: FocusRequester?) {
        if (fr == null) return
        scope.launch {
            repeat(12) {
                if (runCatching { fr.requestFocus() }.isSuccess) return@launch
                delay(40)
            }
        }
    }

    // 调试注入：adb 广播写入 pendingQuery → 变为搜索框内容（MuMu 注键丢键严重，实测用）
    LaunchedEffect(Unit) {
        while (true) {
            SearchDebugBus.pendingQuery?.let {
                SearchDebugBus.pendingQuery = null
                query = it
            }
            delay(200)
        }
    }

    // 拼音词表懒加载（IO 线程，加载完成前候选栏静默隐藏）
    var imeReady by remember { mutableStateOf(PinyinIme.ready()) }
    LaunchedEffect(Unit) {
        if (!imeReady) {
            imeReady = withContext(Dispatchers.IO) { PinyinIme.load(context) }
        }
    }
    // 尾部拼音输入段 → 汉字候选（AOSP 谷歌拼音 native 引擎）
    val trailing = remember(query) { PinyinIme.trailingSegment(query) }

    // ---- 首字母拼音联想（2026-09-05 用户需求：aqgy→爱情公寓 / heysn→花儿与少年）----
    // native 引擎只支持全拼组词；对片库目录建「片名→拼音首字母串」索引，输入尾部
    // 字母段做前缀匹配，命中片名作为候选插在 native 候选之前（上屏后走中文自动搜索）
    var initialsReady by remember { mutableStateOf(PinyinIme.initialsReady()) }
    var titleIndexReady by remember { mutableStateOf(PinyinIme.titleIndexReady()) }
    LaunchedEffect(imeReady) {
        if (imeReady && !initialsReady) {
            initialsReady = withContext(Dispatchers.IO) { PinyinIme.loadInitials() }
        }
        if (imeReady && !titleIndexReady) {
            titleIndexReady = withContext(Dispatchers.IO) { PinyinIme.loadTitleIndex() }
        }
    }
    val catalogItems = Graph.repo.state.collectAsStateWithLifecycle().value.catalog?.dedupedItems.orEmpty()
    // 目录动态索引：兜底词表快照（构建期抓取）之外的新上影片
    var initialsIndex by remember { mutableStateOf<List<Pair<String, VodItem>>>(emptyList()) }
    LaunchedEffect(initialsReady, catalogItems) {
        if (!initialsReady || catalogItems.isEmpty()) return@LaunchedEffect
        initialsIndex = withContext(Dispatchers.Default) {
            catalogItems.mapNotNull { item ->
                val ini = PinyinIme.titleInitials(item.title)
                if (ini.length >= 2) ini to item else null
            }
        }
    }
    val initialsMatches = remember(trailing, initialsIndex, titleIndexReady) {
        if (trailing.length >= 2) {
            val assetHits = if (titleIndexReady) PinyinIme.matchByInitials(trailing, 6) else emptyList()
            val catalogHits = if (initialsIndex.isNotEmpty())
                initialsIndex.filter { it.first.startsWith(trailing) }
                    .sortedBy { it.first.length }.take(4).map { it.second.title }
            else emptyList()
            (assetHits + catalogHits).distinct().take(8)
        } else emptyList()
    }
    // 单行候选：横滑不限个数，放宽到 24（旧 6×6 网格版因纵向空间只给 12）；
    // 首字母联想片名（词表+目录）排在 native 候选之前
    val candidates = remember(trailing, imeReady, initialsMatches) {
        if (imeReady && trailing.isNotEmpty()) {
            (initialsMatches + PinyinIme.candidates(trailing, 24)).distinct().take(24)
        } else emptyList()
    }
    val segmentedPinyin = remember(trailing, imeReady) {
        if (imeReady && trailing.isNotEmpty()) PinyinIme.segmentedPinyin(trailing) else trailing
    }
    // 候选清空（已上屏/清空输入）时把焦点拉回键盘：否则候选节点销毁后焦点会漂移到结果区。
    // 只在「本轮输入确实出现过候选」后生效——BACK 重建组合时不得抢焦点（会盖掉返回落焦恢复）
    var hadCandidates by remember { mutableStateOf(false) }
    if (candidates.isNotEmpty()) hadCandidates = true
    LaunchedEffect(candidates.isEmpty(), hadCandidates) {
        if (candidates.isEmpty() && hadCandidates) {
            kotlinx.coroutines.delay(60)
            runCatching { keyboardFocus.requestFocus() }
        }
    }

    // ---- 全站搜索触发规则（2026-08-29 重设计，修复源站 429 限流根因）----
    //   ① 搜索词 = 去掉尾部未上屏拼音段后的剩余；
    //   ② 搜索词含中文（候选已上屏）→ 防抖后自动搜；
    //   ③ 纯拼音不自动搜（逐键前缀必然触发限流；2026-09-11 键盘换 tvOS 版式后
    //      原「搜索」显式键随旧键盘移除，拼音搜索一律走候选词上屏路径）。
    val searchable = remember(query) {
        val q = query.trim()
        val stripped = if (trailing.isNotEmpty() && q.length > trailing.length)
            q.dropLast(trailing.length).trim() else q
        stripped.ifBlank { q }
    }
    var remote by remember { mutableStateOf<List<VodItem>>(emptyList()) }
    var remoteLoading by remember { mutableStateOf(false) }
    var lastSearched by remember { mutableStateOf("") }
    LaunchedEffect(searchable) {
        if (searchable.isEmpty()) {
            remote = emptyList(); remoteLoading = false
            lastSearched = ""   // 清空后重输同一词也要重搜
            return@LaunchedEffect
        }
        if (searchable == lastSearched) return@LaunchedEffect
        delay(450)
        if (!isActive) return@LaunchedEffect
        lastSearched = searchable
        remoteLoading = true
        remote = Graph.live.searchRemote(searchable)
        remoteLoading = false
    }
    // v1.19 流畅度：本地目录扫描挪 Default 派发——旧版 remember(query){search(query)}
    // 在组合期主线程对全目录（数千条）做多字段 contains，逐键输入有可感知延迟
    var localHits by remember { mutableStateOf<List<VodItem>>(emptyList()) }
    LaunchedEffect(query) {
        localHits = if (query.isBlank()) emptyList()
        else kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Default) { Graph.repo.search(query) }
    }
    val results = remember(localHits, remote) {
        (localHits + remote).distinctBy { it.id }
    }

    // 键盘焦点守卫：候选/结果变化引发的重组会把焦点从软键盘/候选词上清掉（LOST 后无落点）。
    // 守卫只认「真丢失」：左栏节点 GAIN 即清除标记；LOST 后 600ms 内 query|remote 再变化
    // 且仍无 GAIN，才把焦点拉回【最后失焦的那个键】（不是固定 'a'，否则连打会错位）。
    var keyFocusLostAt by remember { mutableLongStateOf(0L) }
    var lastLostKeyFr by remember { mutableStateOf<androidx.compose.ui.focus.FocusRequester?>(null) }
    val noteKeyLost: (androidx.compose.ui.focus.FocusRequester?) -> Unit = { fr ->
        keyFocusLostAt = android.os.SystemClock.uptimeMillis(); lastLostKeyFr = fr
    }
    val noteKeyGained = { keyFocusLostAt = 0L; lastLostKeyFr = null }
    LaunchedEffect(query, remote) {
        if (query.isEmpty()) return@LaunchedEffect
        kotlinx.coroutines.delay(150)
        if (keyFocusLostAt > 0 && android.os.SystemClock.uptimeMillis() - keyFocusLostAt < 600) {
            android.util.Log.d("OptimalTV", "search: keyboard focus dropped on update — restore")
            keyFocusLostAt = 0
            val fr = lastLostKeyFr
            lastLostKeyFr = null
            runCatching { (fr ?: keyboardFocus).requestFocus() }
        }
    }

    // 结果分类（需求④）：目录内条目直接按频道归类；目录外拉详情归类（带缓存，前 24 条）
    var catMap by remember { mutableStateOf<Map<String, String>>(emptyMap()) }
    LaunchedEffect(results) {
        if (results.isEmpty()) return@LaunchedEffect
        val map = HashMap<String, String>()
        results.take(24).forEach { item ->
            val cat = Graph.repo.itemById(item.id)?.categoryId?.let { cid ->
                when (cid.substringAfterLast(':')) {
                    "1" -> "电影"; "2" -> "电视剧"; "3" -> "动漫"; "4" -> "综艺"; "6" -> "短剧"
                    else -> null
                }
            } ?: run {
                val d = runCatching { Graph.repo.resolveDetail(item.id) }.getOrNull() ?: return@run null
                classifyVod(d.meta, d.episodes.size)
            }
            if (cat != null) map[item.id] = cat
        }
        catMap = map
    }
    var selectedCat by rememberSaveable { mutableStateOf("全部") }
    val shown = remember(results, catMap, selectedCat) {
        if (selectedCat == "全部") results
        else results.filter { catMap[it.id] == selectedCat }
    }
    val resultsGridState = androidx.compose.foundation.lazy.grid.rememberLazyGridState()
    // 返回落焦恢复（需求 影视#6-2）：结果到齐后回到上次聚焦的结果卡
    LaunchedEffect(shown) {
        val key = returnKey ?: return@LaunchedEffect
        if (restoredFocus || shown.isEmpty()) return@LaunchedEffect
        restoredFocus = true
        val target = if (key == shown.firstOrNull()?.id) firstResultFocus else cardReg.fr(key)
        android.util.Log.d("OptimalTV", "search return-focus: key=$key firstId=${shown.firstOrNull()?.id}")
        repeat(40) { attempt ->
            val ok = runCatching { target.requestFocus() }.isSuccess
            if (ok) {
                android.util.Log.d("OptimalTV", "search return-focus OK attempt=$attempt")
                return@LaunchedEffect
            }
            delay(100)
        }
        android.util.Log.d("OptimalTV", "search return-focus FAILED → keyboard")
        runCatching { keyboardFocus.requestFocus() }
    }

    // ---- 区域间定向跳转（需求⑤：严格相邻，只到最近的下个区域）----
    // 键盘 DOWN → 候选词行（无候选时跳过它直接到分类 tab；都没有则封锁）
    val keyDown: () -> Unit = {
        when {
            candidates.isNotEmpty() -> focusTarget(firstCandFocus)
            results.isNotEmpty() -> focusTarget(firstTabFocus)
            else -> {}
        }
    }
    // 候选词 UP → 键盘；DOWN → 分类 tab（无结果也封锁，不放行几何搜索）
    val candUp: () -> Unit = { focusTarget(keyboardFocus) }
    val candDown: () -> Unit = { if (results.isNotEmpty()) focusTarget(firstTabFocus) }
    // 分类 tab UP → 候选词行（无候选回键盘）；DOWN → 结果首卡
    val tabUp: () -> Unit = { if (candidates.isNotEmpty()) focusTarget(firstCandFocus) else focusTarget(keyboardFocus) }
    val tabDown: () -> Unit = { if (shown.isNotEmpty()) focusTarget(firstResultFocus) }

    // 返回键回上一页（标签栏仅键盘到顶 UP 定向可达，方向键不会误入）
    androidx.activity.compose.BackHandler { nav.popBackStack() }

    Column(Modifier.fillMaxSize().background(Color(0xFF2C2C2E))) {
        // 顶部标签页（需求④：保持不变）；tab 上 DOWN 直达软键盘
        MainTabBar(
            "",
            { key -> com.qiubo.optimaltv.ui.components.navigateToTab(nav, key) },
            { nav.navigate("search") { launchSingleTop = true } },
            initialFocus = null,
            interactive = true,
            contentDownFocus = keyboardFocus,
            searchChipFocus = tabFocus,
            autoHide = true,   // v1.16：光标下移进键盘/内容区隐藏顶栏，回顶部显示
        )

        Column(Modifier.fillMaxSize().padding(horizontal = 70f.sx(s))) {
            // ---- tvOS 18 键盘条（2026-09-11 一比一复刻 Figma「tvOS 18 UI Kit」node 5-11799）----
            // v1.13 语义沿用：UP 定向回顶部标签栏搜索 chip，DOWN 定向到候选词/分类 tab
            //（声明式 LocalNavOverride，区域内所有键共享；失败也消费封锁）
            CompositionLocalProvider(
                LocalNavOverride provides { dir: Int ->
                    when (dir) {
                        NAV_UP -> { runCatching { tabFocus.requestFocus() }; true }
                        NAV_DOWN -> { keyDown(); true }
                        else -> false
                    }
                },
            ) {
                TvosSearchKeyboard(
                    query = query,
                    pinyinHint = if (candidates.isNotEmpty()) segmentedPinyin else null,
                    numeric = numeric,
                    s = s,
                    keyboardFocus = keyboardFocus,
                    noteKeyGained = noteKeyGained,
                    noteKeyLost = noteKeyLost,
                    onChar = { query += it.toString() },
                    onDelete = { if (query.isNotEmpty()) query = query.dropLast(1) },
                    onToggleNumeric = { numeric = !numeric },
                )
            }

            // ---- 候选词一行（需求④：软键盘下方一行，可左右滚动）----
            if (candidates.isNotEmpty()) {
                val candListState = rememberLazyListState()
                val candScroll = remember(candListState) { lazyNavContainer(candListState) }
                CompositionLocalProvider(
                    LocalNavOverride provides { dir: Int ->
                        when (dir) {
                            NAV_UP -> { candUp(); true }
                            NAV_DOWN -> { candDown(); true }
                            else -> false
                        }
                    },
                ) {
                    ProvideNavScrolls(horizontal = candScroll) {
                        LazyRow(
                            state = candListState,
                            horizontalArrangement = Arrangement.spacedBy(14f.sx(s)),
                            contentPadding = PaddingValues(horizontal = 4f.sx(s)),
                            modifier = Modifier
                                .padding(top = 22f.sx(s))
                                .fillMaxWidth(),
                        ) {
                    items(candidates.size, key = { i -> "cand-$i-${candidates[i]}" }) { i ->
                        val cand = candidates[i]
                        var candFocused by remember { mutableStateOf(false) }
                        Box(
                            contentAlignment = Alignment.Center,
                            modifier = Modifier
                                .then(if (i == 0) Modifier.focusRequester(firstCandFocus) else Modifier)
                                .dpadFocusable(
                                    scaleFocused = 1.08f, focusedBg = OtvColors.White,
                                    focusedBgRadius = 24f.sx(s),
                                    onFocusedChange = {
                                        candFocused = it
                                        if (it) noteKeyGained() else noteKeyLost(null)
                                    },
                                ) {
                                    query = PinyinIme.applyCandidate(query, cand)
                                }
                                .background(
                                    if (candFocused) OtvColors.White else Color(0x24FFFFFF),
                                    RoundedCornerShape(24f.sx(s)),
                                )
                                .padding(horizontal = 20f.sx(s), vertical = 10f.sx(s)),
                        ) {
                            Text(
                                cand,
                                color = if (candFocused) OtvColors.Bg else OtvColors.White,
                                fontSize = 28f.sxs(s),
                                fontWeight = FontWeight.SemiBold,
                                maxLines = 1,
                            )
                        }
                    }
                        }
                    } // ProvideNavScrolls(candScroll)
                } // CompositionLocalProvider(候选词区)
            }

            // ---- 结果分类 tab（需求④：全部/电影/电视剧/动漫/短剧，聚焦即切换）----
            if (results.isNotEmpty()) {
                CompositionLocalProvider(
                    // 需求⑤：UP 回候选词/键盘，DOWN 进结果首卡（失败也消费封锁）
                    LocalNavOverride provides { dir: Int ->
                        when (dir) {
                            NAV_UP -> { tabUp(); true }
                            NAV_DOWN -> { tabDown(); true }
                            else -> false
                        }
                    },
                ) {
                Row(
                    horizontalArrangement = Arrangement.spacedBy(14f.sx(s)),
                    modifier = Modifier
                        .padding(top = 26f.sx(s)),
                ) {
                    RESULT_CATS.forEachIndexed { ci, catName ->
                        val count = if (catName == "全部") results.size else results.count { catMap[it.id] == catName }
                        val active = selectedCat == catName
                        var tabFocused by remember { mutableStateOf(false) }
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            modifier = Modifier
                                .then(if (ci == 0) Modifier.focusRequester(firstTabFocus) else Modifier)
                                .dpadFocusable(
                                    scaleFocused = 1.05f, focusedBg = OtvColors.White,
                                    focusedBgRadius = 26f.sx(s),
                                    onFocusedChange = { tabFocused = it },
                                    autoSelect = true,
                                ) { selectedCat = catName }
                                .background(
                                    if (tabFocused) OtvColors.White else if (active) OtvColors.White.copy(alpha = 0.22f) else Color(0x24FFFFFF),
                                    RoundedCornerShape(26f.sx(s)),
                                )
                                .padding(horizontal = 24f.sx(s), vertical = 10f.sx(s)),
                        ) {
                            Text(
                                catName,
                                color = if (tabFocused) OtvColors.Bg else if (active) OtvColors.White else OtvColors.White.copy(alpha = 0.7f),
                                fontSize = 26f.sxs(s), fontWeight = FontWeight.SemiBold,
                            )
                            if (count > 0) {
                                Spacer(Modifier.width(8f.sx(s)))
                                Text(
                                    "$count",
                                    color = if (tabFocused) OtvColors.Bg.copy(alpha = 0.6f) else OtvColors.White50,
                                    fontSize = 20f.sxs(s),
                                )
                            }
                        }
                    }
                }
                } // CompositionLocalProvider(分类 tab 区)
            }

            // ---- 搜索结果（需求④：候选词下方区域，竖版海报卡 7 列）----
            if (shown.isEmpty()) {
                if (query.isNotBlank()) Text(
                    when {
                        results.isEmpty() && remoteLoading -> "搜索中…"
                        results.isEmpty() -> "没有匹配的影片"
                        else -> "该分类暂无结果"
                    },
                    color = OtvColors.White50, fontSize = 26f.sxs(s),
                    modifier = Modifier.padding(top = 34f.sx(s)),
                )
            } else {
                val resultScroll = remember(resultsGridState) { lazyGridNavContainer(resultsGridState) }
                ProvideNavScrolls(vertical = resultScroll) {
                    LazyVerticalGrid(
                        state = resultsGridState,
                        columns = GridCells.Fixed(7),
                        contentPadding = PaddingValues(top = 24f.sx(s), bottom = 60f.sx(s)),
                        horizontalArrangement = Arrangement.spacedBy(20f.sx(s)),
                        verticalArrangement = Arrangement.spacedBy(36f.sx(s)),
                        modifier = Modifier
                            .weight(1f)
                            .fillMaxWidth(),
                    ) {
                        // key 用纯 id（results 已 distinctBy id 保证唯一）：结果集变化时
                        // 节点按 id 复用，避免全量销毁重组引发焦点丢失风暴（2026-08-31）
                        items(shown.size, key = { i -> shown[i].id }) { i ->
                            ResultCard(
                                nav, shown[i], s, cardReg,
                                externalFocus = if (i == 0) firstResultFocus else null,
                            )
                        }
                    }
                }
            }
        }
    }
}

/** 竖版结果卡（与片库同款竖版海报；片名在卡下，右下角 remark 角标同片库样式）
 *  需求②：角标字号加大黑底压扁（同 AllScreen.VcardCard） */
@Composable
private fun ResultCard(
    nav: NavController,
    item: VodItem,
    s: Float,
    cardReg: FocusRegistry? = null,
    externalFocus: FocusRequester? = null,
) {
    Column(Modifier.width(232f.sx(s))) {
        Box(
            Modifier
                .dpadFocusable(
                    scaleFocused = 1.05f,
                    onFocusedChange = { if (it) ReturnFocus.mark("search", item.id) },
                    externalFocusRequester = externalFocus ?: cardReg?.fr(item.id),
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
        Text(
            item.title, color = OtvColors.White, fontSize = 24f.sxs(s), fontWeight = FontWeight.Medium,
            maxLines = 1, overflow = TextOverflow.Ellipsis,
        )
    }
}

/**
 * tvOS 18 搜索键盘条（一比一复刻 Figma「tvOS 18 UI Kit」node 5-11799，1920×295 设计系）：
 * 标题行（放大镜 87×86 + Search/输入 76px Bold 白30% + 右侧 Hold to dictate 38px）→
 * 白30% 分割线（条内 y=294，x=10..1770）→ 键行（123 | SPACE | a-z | ⌫）。
 * 外层 Column 已含 70px 页边距，以下 x 坐标均为设计稿减 70 后的条内实测值。
 */
@Composable
private fun TvosSearchKeyboard(
    query: String,
    pinyinHint: String?,
    numeric: Boolean,
    s: Float,
    keyboardFocus: FocusRequester,
    noteKeyGained: () -> Unit,
    noteKeyLost: (androidx.compose.ui.focus.FocusRequester?) -> Unit,
    onChar: (Char) -> Unit,
    onDelete: () -> Unit,
    onToggleNumeric: () -> Unit,
) {
    Box(Modifier.fillMaxWidth().height(295f.sx(s))) {
        // ---- 标题行（放大镜盒 y=55 / 标题 y=49 / Hold 行 y=77）----
        Row(Modifier.offset(x = 0f.sx(s), y = 49f.sx(s)).fillMaxWidth()) {
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier.offset(x = 0f.sx(s), y = 6f.sx(s)).size(87f.sx(s), 86f.sx(s)),
            ) {
                Icon(
                    painterResource(R.drawable.ic_tvos_kbd_search), contentDescription = null,
                    tint = OtvColors.White30, modifier = Modifier.size(70f.sx(s), 71f.sx(s)),
                )
            }
            Spacer(Modifier.width(8f.sx(s)))
            Text(
                query.ifBlank { "Search" },
                color = if (query.isBlank()) OtvColors.White30 else OtvColors.White,
                fontSize = 76f.sxs(s), fontWeight = FontWeight.Bold,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            // 输入中显示引擎切分的拼音（App 专属提示，沿用旧输入行右侧位置；与 Hold 行留距）
            if (pinyinHint != null) {
                Spacer(Modifier.width(18f.sx(s)))
                Text(
                    "$pinyinHint ›",
                    color = OtvColors.White50, fontSize = 26f.sxs(s), maxLines = 1,
                    modifier = Modifier.align(Alignment.CenterVertically).padding(end = 24f.sx(s)),
                )
            }
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.offset(y = 28f.sx(s)).padding(end = 15f.sx(s)),
            ) {
                Text("Hold", color = OtvColors.White30, fontSize = 38f.sxs(s), fontWeight = FontWeight.Medium)
                Spacer(Modifier.width(8f.sx(s)))
                Icon(
                    painterResource(R.drawable.ic_tvos_kbd_mic), contentDescription = null,
                    tint = OtvColors.White30, modifier = Modifier.size(38f.sx(s)),
                )
                Spacer(Modifier.width(8f.sx(s)))
                Text("to dictate", color = OtvColors.White30, fontSize = 38f.sxs(s), fontWeight = FontWeight.Medium)
            }
        }
        // ---- 分割线（白30%，高 1）----
        Box(
            Modifier
                .offset(x = 10f.sx(s), y = 294f.sx(s))
                .size(1760f.sx(s), 1f.sx(s))
                .background(OtvColors.White30),
        )
        // ---- 键行：字母行 y=185 高 64；功能键 y=203 高 38（中线与字母行对齐，间隙 30）----
        TvActionKey(
            if (numeric) "ABC" else "123", x = 78f, y = 203f, w = 59f, h = 38f, s = s,
            onFocusChange = { if (it) noteKeyGained() else noteKeyLost(null) },
        ) { onToggleNumeric() }
        TvActionKey(
            "SPACE", x = 167f, y = 203f, w = 89f, h = 38f, s = s,
            onFocusChange = { if (it) noteKeyGained() else noteKeyLost(null) },
        ) { onChar(' ') }
        val rowChars: List<Char> = if (numeric) ('0'..'9').toList() else ('a'..'z').toList()
        rowChars.forEachIndexed { i, k ->
            // 'a' 键自带键盘区落焦句柄（初始落焦/守卫恢复目标）
            val keyFr = remember { FocusRequester() }
            val handle = if (!numeric && k == 'a') keyboardFocus else keyFr
            TvKey(
                k.toString(), x = 286f + 54f * i, y = 185f, s = s, focus = handle,
                onFocusChange = { if (it) noteKeyGained() else noteKeyLost(handle) },
            ) { onChar(k) }
        }
        TvDeleteKey(
            x = 1710f, y = 185f, s = s,
            onFocusChange = { if (it) noteKeyGained() else noteKeyLost(null) },
        ) { onDelete() }
    }
}

/** tvOS 字母/数字键（54×64 设计实测）：无底、48px Medium 白50%（行高 64 与键盒同高）；聚焦白底黑字胶囊 */
@Composable
private fun TvKey(
    label: String,
    x: Float,
    y: Float,
    s: Float,
    focus: FocusRequester? = null,
    onFocusChange: ((Boolean) -> Unit)? = null,
    onTap: () -> Unit,
) {
    var f by remember { mutableStateOf(false) }
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .offset(x = x.sx(s), y = y.sx(s))
            .dpadFocusable(
                scaleFocused = 1.1f, focusedBg = OtvColors.White,
                focusedBgRadius = 14f.sx(s),
                externalFocusRequester = focus,
                onFocusedChange = { f = it; onFocusChange?.invoke(it) },
            ) { onTap() }
            .size(54f.sx(s), 64f.sx(s)),
    ) {
        Text(
            label,
            color = if (f) OtvColors.Bg else OtvColors.White50,
            fontSize = 48f.sxs(s), fontWeight = FontWeight.Medium,
            lineHeight = 64f.sxs(s), maxLines = 1,
        )
    }
}

/** tvOS 功能键（123/ABC 59×38、SPACE 89×38，白50% 胶囊 r7 黑字 20px 设计实测）；聚焦白底变实 */
@Composable
private fun TvActionKey(
    label: String,
    x: Float,
    y: Float,
    w: Float,
    h: Float,
    s: Float,
    focus: FocusRequester? = null,
    onFocusChange: ((Boolean) -> Unit)? = null,
    onTap: () -> Unit,
) {
    var f by remember { mutableStateOf(false) }
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .offset(x = x.sx(s), y = y.sx(s))
            .dpadFocusable(
                scaleFocused = 1.1f, focusedBg = OtvColors.White,
                focusedBgRadius = 7f.sx(s),
                externalFocusRequester = focus,
                onFocusedChange = { f = it; onFocusChange?.invoke(it) },
            ) { onTap() }
            .background(OtvColors.White50, RoundedCornerShape(7f.sx(s)))
            .size(w.sx(s), h.sx(s)),
    ) {
        Text(
            label,
            color = OtvColors.Bg,
            fontSize = 20f.sxs(s), fontWeight = FontWeight.Medium, maxLines = 1,
        )
    }
}

/** tvOS ⌫ 键（54×64 键盒，SF Symbols delete.left 矢量 52×44 居中，白50%）；聚焦白底黑图标 */
@Composable
private fun TvDeleteKey(
    x: Float,
    y: Float,
    s: Float,
    focus: FocusRequester? = null,
    onFocusChange: ((Boolean) -> Unit)? = null,
    onTap: () -> Unit,
) {
    var f by remember { mutableStateOf(false) }
    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .offset(x = x.sx(s), y = y.sx(s))
            .dpadFocusable(
                scaleFocused = 1.1f, focusedBg = OtvColors.White,
                focusedBgRadius = 14f.sx(s),
                externalFocusRequester = focus,
                onFocusedChange = { f = it; onFocusChange?.invoke(it) },
            ) { onTap() }
            .size(54f.sx(s), 64f.sx(s)),
    ) {
        Icon(
            painterResource(R.drawable.ic_tvos_kbd_delete), contentDescription = "删除",
            tint = if (f) OtvColors.Bg else OtvColors.White50,
            modifier = Modifier.size(52f.sx(s), 44f.sx(s)),
        )
    }
}

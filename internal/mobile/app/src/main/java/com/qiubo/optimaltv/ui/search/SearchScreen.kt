package com.qiubo.optimaltv.ui.search

import androidx.activity.compose.BackHandler
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
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.foundation.border
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.foundation.focusable
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.navigation.NavController
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.ui.components.MainTabBar
import com.qiubo.optimaltv.ui.components.MobileVodCard
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.PosterPlaceholder
import com.qiubo.optimaltv.ui.components.isScrolledFromTop
import com.qiubo.optimaltv.ui.components.navigateToTab
import com.qiubo.optimaltv.ui.components.tapCard
import com.qiubo.optimaltv.ui.components.MobileBackButton
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.net.URLEncoder

/**
 * 搜索页（v1.18 与 TV 版 1:1 同款版式）：
 * 顶部标签栏 → 搜索输入行（⌕ + 输入框，触屏用系统输入法替代 TV 版自研拼音软键盘）→
 * 结果分类 tab（全部/电影/电视剧/动漫/短剧）→ 结果网格（7 列竖版海报卡）。
 * 搜索链路与 TV 版一致——目录内过滤 + 全站实时搜索（10 分钟缓存、450ms 防抖、
 * 空结果三级重试 3s/6s）；含中文自动搜、纯字母走输入法搜索键（防 429 限流同源规则）。
 */
/** 搜索结果分类 tab（需求④）。「综艺」补齐：6 个，竖屏每行 3 个刚好两整行。 */
internal val RESULT_CATS = listOf("全部", "电影", "电视剧", "动漫", "综艺", "短剧")

/** D-pad 邻居索引；水平键留在当前行，竖向键按列移动，残行向最近的卡片收敛。 */
internal enum class SearchResultFocusDirection { Left, Right, Up, Down }

internal fun mobileSearchResultFocusTarget(
    index: Int,
    resultCount: Int,
    columns: Int,
    direction: SearchResultFocusDirection,
): Int? {
    if (columns <= 0 || index !in 0 until resultCount) return null
    val row = index / columns
    val column = index % columns
    return when (direction) {
        SearchResultFocusDirection.Left -> index.takeIf { column > 0 }?.minus(1)
        SearchResultFocusDirection.Right ->
            (index + 1).takeIf { column + 1 < columns && it < resultCount }
        SearchResultFocusDirection.Up -> (index - columns).takeIf { row > 0 }
        SearchResultFocusDirection.Down -> {
            val nextRowStart = (row + 1) * columns
            if (nextRowStart >= resultCount) null
            else (nextRowStart + column).coerceAtMost(resultCount - 1)
        }
    }
}
/** 详情 meta/集数 → 分类（源站频道 tag：欧美剧/国产剧等以「剧」结尾的类型 + 综艺/动漫/短剧直配） */
private val TV_TAG_RE = Regex("欧美剧|国产剧|香港剧|台湾剧|韩国剧|日本剧|海外剧|泰剧|美剧|英剧|连续剧|电视剧|网剧")

/** 调试词注入通道（adb am start --es otvDebugQuery，仅 debug 构建有写入方） */
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

/** categoryId → 分类；`douban:<mediaType>` 与旧 `hhkan:<频道号>` 都认，认不出返回 null。 */
internal fun searchResultCategory(categoryId: String): String? =
    when (categoryId.substringAfterLast(':').trim()) {
        "movie", "1" -> "电影"
        "tv", "2" -> "电视剧"
        "anime", "3" -> "动漫"
        "variety", "4" -> "综艺"
        "short", "6" -> "短剧"
        else -> null
    }

/** 认不出分类的条目才拉详情兜底，最多这么多条。 */
private const val DETAIL_FALLBACK_LIMIT = 24

/**
 * 合并目录内命中与后端结果：remote 在前、local 补充，同 id 取先出现的那条。
 *
 * 本地目录按「首次出现的浏览频道」打 categoryId（那是分区不是真实分类），
 * local 排在前面会让错分类的副本遮住后端已认定 douban:anime 的同名条目。
 */
internal fun mergeSearchResults(
    localHits: List<com.qiubo.optimaltv.data.model.VodItem>,
    remote: List<com.qiubo.optimaltv.data.model.VodItem>,
): List<com.qiubo.optimaltv.data.model.VodItem> =
    (remote + localHits).distinctBy { it.id }

/**
 * 搜索结果 → (id → 分类)。categoryId 可认的对全部结果生效；认不出的才走
 * [detailFallback] 拉详情，且最多 [DETAIL_FALLBACK_LIMIT] 条。
 */
internal suspend fun classifySearchResults(
    items: List<com.qiubo.optimaltv.data.model.VodItem>,
    detailFallback: suspend (com.qiubo.optimaltv.data.model.VodItem) -> String?,
): Map<String, String> {
    val map = HashMap<String, String>(items.size)
    var fallbackBudget = DETAIL_FALLBACK_LIMIT
    for (item in items) {
        val known = searchResultCategory(item.categoryId)
        if (known != null) {
            map[item.id] = known
            continue
        }
        if (fallbackBudget <= 0) continue
        fallbackBudget--
        detailFallback(item)?.let { map[item.id] = it }
    }
    return map
}

@Composable
fun SearchScreen(nav: NavController) {
    var query by rememberSaveable { mutableStateOf("") }
    val s = rememberUiScale()
    // 竖屏（2026-09-07 竖屏优化）：侧距 70→40、输入字号 52→40（与网页版 portrait 分支同规格）
    val portrait = androidx.compose.ui.platform.LocalConfiguration.current.orientation ==
        android.content.res.Configuration.ORIENTATION_PORTRAIT
    val bodyPad = if (portrait) 40f else 70f
    val inputSize = if (portrait) 40f else 52f

    // 调试注入：am start --es otvDebugQuery 写入 pendingQuery → 变为搜索框内容
    LaunchedEffect(Unit) {
        while (true) {
            SearchDebugBus.pendingQuery?.let {
                SearchDebugBus.pendingQuery = null
                query = it
            }
            delay(200)
        }
    }

    // ---- 全站搜索触发规则（与 TV 版同源，修复源站 429 限流根因）----
    //   ① 搜索词含中文（输入法已上屏）→ 防抖后自动搜；
    //   ② 纯字母不自动搜（逐键前缀必然触发限流）——输入法「搜索」键显式搜。
    var explicitSearch by remember { mutableIntStateOf(0) }
    val searchable = remember(query) { query.trim() }
    var remote by remember(searchable) { mutableStateOf<List<VodItem>>(emptyList()) }
    var remoteLoading by remember { mutableStateOf(false) }
    var lastSearched by remember(searchable) { mutableStateOf("") }
    LaunchedEffect(searchable, explicitSearch) {
        if (searchable.isEmpty()) {
            remote = emptyList(); remoteLoading = false
            lastSearched = ""   // 清空后重输同一词也要重搜
            return@LaunchedEffect
        }
        remoteLoading = false
        if (explicitSearch == 0 && searchable == lastSearched) return@LaunchedEffect
        delay(250)
        if (!isActive) return@LaunchedEffect
        remoteLoading = true
        try {
            val found = Graph.live.searchRemote(searchable)
            if (isActive) {
                remote = found
                lastSearched = searchable
            }
        } finally {
            if (isActive) remoteLoading = false
        }
    }
    // 版本化 SQLite 搜索在 Repository 的 IO dispatcher 执行；种子目录回退扫描在 Default 执行。
    var localHits by remember(query) { mutableStateOf<List<VodItem>>(emptyList()) }
    LaunchedEffect(query) {
        localHits = if (query.isBlank()) emptyList()
        else Graph.repo.search(query)
    }
    val results = remember(localHits, remote) {
        mergeSearchResults(localHits, remote)
    }

    // 结果分类（需求④）：认 categoryId → 认不出才拉详情兜底（见 classifySearchResults）
    var catMap by remember { mutableStateOf<Map<String, String>>(emptyMap()) }
    LaunchedEffect(results) {
        if (results.isEmpty()) return@LaunchedEffect
        catMap = classifySearchResults(results) { item ->
            runCatching { Graph.repo.resolveDetail(item.id) }.getOrNull()?.let { d ->
                classifyVod(d.meta, d.episodes.size)
            }
        }
    }
    var selectedCat by rememberSaveable { mutableStateOf("全部") }
    val shown = remember(results, catMap, selectedCat) {
        if (selectedCat == "全部") results
        else results.filter { catMap[it.id] == selectedCat }
    }

    // 顶栏下潜：结果区离开绝对顶部即隐藏、回顶显示
    val resultsGridState = rememberLazyGridState()
    val resultColumns = if (portrait) 3 else com.qiubo.optimaltv.ui.theme.rememberRowColumns(
        cardW = 232f, gap = 20f, sidePad = bodyPad,
    )
    val resultFocusRequesters = remember(shown) {
        shown.associate { it.id to FocusRequester() }
    }
    val scope = rememberCoroutineScope()
    var focusedResultId by remember { mutableStateOf<String?>(null) }
    var pendingResultFocusId by remember { mutableStateOf<String?>(null) }

    fun requestSearchResultFocus(index: Int) {
        val item = shown.getOrNull(index) ?: return
        if (focusedResultId == item.id || pendingResultFocusId == item.id) return
        val requester = resultFocusRequesters[item.id] ?: return
        pendingResultFocusId = item.id
        scope.launch {
            try {
                // Bring lazy items into composition before requesting focus. This also makes
                // 20-result lists navigable when the next row has not yet been composed.
                resultsGridState.scrollToItem(index)
                repeat(12) {
                    runCatching { requester.requestFocus() }
                    if (focusedResultId == item.id) return@launch
                    delay(40)
                }
            } finally {
                if (pendingResultFocusId == item.id) pendingResultFocusId = null
            }
        }
    }

    LaunchedEffect(query) {
        selectedCat = "全部"
        resultsGridState.scrollToItem(0)
    }
    val chromeGone by remember(resultsGridState) {
        derivedStateOf {
            isScrolledFromTop(
                resultsGridState.firstVisibleItemIndex,
                resultsGridState.firstVisibleItemScrollOffset,
            )
        }
    }

    var hint by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(hint) { if (hint != null) { delay(2500); hint = null } }

    BackHandler { nav.popBackStack() }

    Box(Modifier.fillMaxSize().background(Color(0xFF2C2C2E))) {
        Column(Modifier.fillMaxSize()) {
            // 顶部标签页（需求④：保持不变）；搜索页无所在 tab（currentKey 空，与 TV 版同构）
            MainTabBar(
                "",
                { key -> navigateToTab(nav, key) },
                { nav.navigate("search") { launchSingleTop = true } },
                hidden = chromeGone,
            )

        Column(
            Modifier
                .fillMaxSize()
                .padding(horizontal = bodyPad.sx(s)),
        ) {
            // ---- 搜索输入行（需求④版式：⌕ + 52 Bold 输入；触屏直接唤系统输入法）----
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .padding(top = 18f.sx(s))
                    .fillMaxWidth(),
            ) {
                Text("⌕", color = OtvColors.White70, fontSize = 54f.sxs(s))
                Spacer(Modifier.width(22f.sx(s)))
                Box(Modifier.weight(1f), contentAlignment = Alignment.CenterStart) {
                    if (query.isBlank()) {
                        Text(
                            "Search",
                            color = OtvColors.White50,
                            fontSize = inputSize.sxs(s), fontWeight = FontWeight.Bold,
                            maxLines = 1,
                        )
                    }
                    BasicTextField(
                        value = query,
                        onValueChange = { query = it },
                        singleLine = true,
                        textStyle = androidx.compose.ui.text.TextStyle(
                            color = OtvColors.White,
                            fontSize = inputSize.sxs(s),
                            fontWeight = FontWeight.Bold,
                        ),
                        cursorBrush = SolidColor(OtvColors.White),
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                        // 「搜索」键 = 显式搜索当前输入（纯拼音/字母也支持——源站支持拼音关键词搜索）
                        keyboardActions = KeyboardActions(
                            onSearch = { if (searchable.isNotEmpty()) { lastSearched = ""; explicitSearch++ } },
                        ),
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                // 输入中：清空钮（对应 TV 版键盘「清空」键）
                if (query.isNotBlank()) {
                    Spacer(Modifier.width(18f.sx(s)))
                    Box(
                        contentAlignment = Alignment.Center,
                        modifier = Modifier
                            .clip(RoundedCornerShape(24f.sx(s)))
                            .tapCard { query = "" }
                            .background(Color(0x24FFFFFF), RoundedCornerShape(24f.sx(s)))
                            .padding(horizontal = 20f.sx(s), vertical = 10f.sx(s)),
                    ) {
                        Text("清空", color = OtvColors.White55, fontSize = 26f.sxs(s), fontWeight = FontWeight.SemiBold)
                    }
                }
            }

            // ---- 结果分类 tab（核心入口竖屏分行全量显示；横屏保持单行）----
            if (results.isNotEmpty()) {
                val tabRows = if (portrait) RESULT_CATS.chunked(3) else listOf(RESULT_CATS)
                Column(
                    verticalArrangement = Arrangement.spacedBy(10f.sx(s)),
                    modifier = Modifier.padding(top = 26f.sx(s)),
                ) {
                    tabRows.forEachIndexed { rowIndex, tabRow ->
                        Row(
                            horizontalArrangement = Arrangement.spacedBy(12f.sx(s)),
                            modifier = Modifier
                                .fillMaxWidth()
                                .then(
                                    if (rowIndex == tabRows.lastIndex) {
                                        Modifier.onPreviewKeyEvent { event ->
                                            if (event.type == KeyEventType.KeyDown &&
                                                event.key == Key.DirectionDown &&
                                                shown.isNotEmpty()
                                            ) {
                                                requestSearchResultFocus(0)
                                                true
                                            } else false
                                        }
                                    } else Modifier,
                                ),
                        ) {
                            tabRow.forEach { catName ->
                                val count = if (catName == "全部") results.size else results.count { catMap[it.id] == catName }
                                val active = selectedCat == catName
                                val chipModifier = if (portrait) Modifier.weight(1f) else Modifier
                                Row(
                                    horizontalArrangement = Arrangement.Center,
                                    verticalAlignment = Alignment.CenterVertically,
                                    modifier = chipModifier
                                        .clip(RoundedCornerShape(26f.sx(s)))
                                        .tapCard { selectedCat = catName }
                                        .background(
                                            if (active) OtvColors.White.copy(alpha = 0.22f) else Color(0x24FFFFFF),
                                            RoundedCornerShape(26f.sx(s)),
                                        )
                                        .padding(horizontal = if (portrait) 10f.sx(s) else 24f.sx(s), vertical = 6f.sx(s)),
                                ) {
                                    Text(
                                        catName,
                                        color = if (active) OtvColors.White else OtvColors.White.copy(alpha = 0.7f),
                                        fontSize = (if (portrait) 23f else 26f).sxs(s), fontWeight = FontWeight.SemiBold,
                                        maxLines = 1,
                                    )
                                    if (count > 0) {
                                        Spacer(Modifier.width(6f.sx(s)))
                                        Text(
                                            "$count",
                                            color = OtvColors.White50,
                                            fontSize = (if (portrait) 18f else 20f).sxs(s),
                                            maxLines = 1,
                                        )
                                    }
                                }
                            }
                            if (portrait) repeat(3 - tabRow.size) { Spacer(Modifier.weight(1f)) }
                        }
                    }
                }
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
                LazyVerticalGrid(
                    state = resultsGridState,
                    columns = GridCells.Fixed(resultColumns),
                    contentPadding = PaddingValues(top = 24f.sx(s), bottom = 60f.sx(s)),
                    horizontalArrangement = Arrangement.spacedBy((if (portrait) 12f else 20f).sx(s)),
                    verticalArrangement = Arrangement.spacedBy((if (portrait) 28f else 36f).sx(s)),
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxWidth(),
                ) {
                    // key 用纯 id（results 已 distinctBy id 保证唯一）
                    items(shown.size, key = { i -> shown[i].id }) { i ->
                        val item = shown[i]
                        val openResult = {
                            nav.navigate("detail/" + URLEncoder.encode(item.id, "UTF-8"))
                        }
                        val requester = resultFocusRequesters[item.id]
                        MobileVodCard(
                            item = item,
                            s = s,
                            compact = portrait,
                            modifier = Modifier
                                .focusRequester(requireNotNull(requester))
                                .onFocusChanged { state ->
                                    if (state.hasFocus) focusedResultId = item.id
                                    else if (focusedResultId == item.id) focusedResultId = null
                                }
                                .onPreviewKeyEvent { event ->
                                    if (event.type != KeyEventType.KeyDown) return@onPreviewKeyEvent false
                                    when (event.key) {
                                        Key.Enter, Key.DirectionCenter -> {
                                            openResult()
                                            true
                                        }
                                        Key.DirectionLeft,
                                        Key.DirectionRight,
                                        Key.DirectionUp,
                                        Key.DirectionDown -> {
                                            val direction = when (event.key) {
                                                Key.DirectionLeft -> SearchResultFocusDirection.Left
                                                Key.DirectionRight -> SearchResultFocusDirection.Right
                                                Key.DirectionUp -> SearchResultFocusDirection.Up
                                                else -> SearchResultFocusDirection.Down
                                            }
                                            val target = mobileSearchResultFocusTarget(
                                                index = i,
                                                resultCount = shown.size,
                                                columns = resultColumns,
                                                direction = direction,
                                            )
                                            if (target != null) requestSearchResultFocus(target)
                                            target != null || direction == SearchResultFocusDirection.Left ||
                                                direction == SearchResultFocusDirection.Right
                                        }
                                        else -> false
                                    }
                                }
                                .focusable()
                                .graphicsLayer {
                                    val focused = focusedResultId == item.id
                                    val scale = if (focused) 1.04f else 1f
                                    scaleX = scale
                                    scaleY = scale
                                }
                                .then(
                                    if (focusedResultId == item.id) {
                                        Modifier.border(
                                            width = 3f.sx(s),
                                            color = OtvColors.White,
                                            shape = RoundedCornerShape(12f.sx(s)),
                                        )
                                    } else Modifier,
                                ),
                            onClick = openResult,
                        )
                    }
                }
            }
        }
        }
        // 移动① 修复：内容/标签栏之后声明 = 最上层，顶部可点元素不再挡返回钮
        MobileBackButton(onBack = { nav.popBackStack() })
        OtvHint(hint)
    }
}

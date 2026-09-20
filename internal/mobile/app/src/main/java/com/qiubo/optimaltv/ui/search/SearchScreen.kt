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
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
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
import java.net.URLEncoder

/**
 * 搜索页（v1.18 与 TV 版 1:1 同款版式）：
 * 顶部标签栏 → 搜索输入行（⌕ + 输入框，触屏用系统输入法替代 TV 版自研拼音软键盘）→
 * 结果分类 tab（全部/电影/电视剧/动漫/短剧）→ 结果网格（7 列竖版海报卡）。
 * 搜索链路与 TV 版一致——目录内过滤 + 全站实时搜索（10 分钟缓存、450ms 防抖、
 * 空结果三级重试 3s/6s）；含中文自动搜、纯字母走输入法搜索键（防 429 限流同源规则）。
 */
/** 搜索结果分类 tab（需求④：全部/电影/电视剧/动漫/短剧） */
private val RESULT_CATS = listOf("全部", "电影", "电视剧", "动漫", "短剧")

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
    var remote by remember { mutableStateOf<List<VodItem>>(emptyList()) }
    var remoteLoading by remember { mutableStateOf(false) }
    var lastSearched by remember { mutableStateOf("") }
    LaunchedEffect(searchable, explicitSearch) {
        if (searchable.isEmpty()) {
            remote = emptyList(); remoteLoading = false
            lastSearched = ""   // 清空后重输同一词也要重搜
            return@LaunchedEffect
        }
        if (explicitSearch == 0 && searchable == lastSearched) return@LaunchedEffect
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

    // 顶栏下潜：结果区离开绝对顶部即隐藏、回顶显示
    val resultsGridState = rememberLazyGridState()
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

        Column(Modifier.fillMaxSize().padding(horizontal = bodyPad.sx(s))) {
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
                    tabRows.forEach { tabRow ->
                        Row(
                            horizontalArrangement = Arrangement.spacedBy(12f.sx(s)),
                            modifier = Modifier.fillMaxWidth(),
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
                    columns = GridCells.Fixed(
                        if (portrait) 3 else com.qiubo.optimaltv.ui.theme.rememberRowColumns(
                            cardW = 232f, gap = 20f, sidePad = bodyPad,
                        ),
                    ),
                    contentPadding = PaddingValues(top = 24f.sx(s), bottom = 60f.sx(s)),
                    horizontalArrangement = Arrangement.spacedBy((if (portrait) 12f else 20f).sx(s)),
                    verticalArrangement = Arrangement.spacedBy((if (portrait) 28f else 36f).sx(s)),
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxWidth(),
                ) {
                    // key 用纯 id（results 已 distinctBy id 保证唯一）
                    items(shown.size, key = { i -> shown[i].id }) { i ->
                        ResultCard(nav, shown[i], s, compact = portrait)
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

/** 竖版结果卡（与片库同款竖版海报；片名在卡下，右下角 remark 角标同片库样式；触控点卡进详情）
 *  需求②：角标字号加大黑底压扁（同 AllScreen.VcardCard）
 *  竖屏优化：fillMaxWidth 填满网格 cell（1fr 均分，与网页版 poster-grid 同构） */
@Composable
private fun ResultCard(
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

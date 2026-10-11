package com.qiubo.optimaltv.ui.live

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import androidx.compose.animation.togetherWith   // 需求⑦：hero 切换动画用
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.model.MatchItem
import com.qiubo.optimaltv.ui.components.MainTabBar
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.isScrolledFromTop
import com.qiubo.optimaltv.ui.components.navigateToTab
import com.qiubo.optimaltv.ui.components.tapCard
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.net.URLEncoder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/* ---------------- VM（与 TV 版同款逻辑） ---------------- */

data class LiveUiState(
    val loading: Boolean = true,
    val error: String? = null,
    val matches: List<MatchItem> = emptyList(),
    val leagues: List<String> = emptyList(),
)

internal fun snapshotFallbackState(current: LiveUiState, snapshot: List<MatchItem>): LiveUiState? {
    if (snapshot.isEmpty() || current.matches.isNotEmpty() || (!current.loading && current.error == null)) return null
    return current.copy(
        loading = false,
        error = null,
        matches = snapshot,
        leagues = snapshot.map { it.league }.filter { it.isNotBlank() }.distinct(),
    )
}

class LiveViewModel : ViewModel() {
    private val _ui = MutableStateFlow(LiveUiState())
    val ui: StateFlow<LiveUiState> = _ui.asStateFlow()

    /** 上次成功刷新时间 / 刷新进行中标记（需求①：轮询去重，慢抓取期间不叠加请求） */
    private var lastRefreshAt = 0L
    @Volatile private var refreshing = false

    init {
        // v1.10（低端机秒开）：先恢复磁盘快照立即上屏——冷启动链路
        //（后端冷抓期间）不再白屏转圈；后台 refresh 到货后替换
        // v1.19 流畅度：快照读盘+JSON 解析挪 IO（旧版在 VM 构造=组合期主线程同步执行）
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            runCatching { Graph.live.matchesFromSnapshot() }.getOrNull()?.let { cached ->
                while (true) {
                    val current = _ui.value
                    val restored = snapshotFallbackState(current, cached) ?: break
                    if (_ui.compareAndSet(current, restored)) {
                        com.qiubo.optimaltv.OtvLog.i("matches 快照恢复上屏 ${cached.size} 场（后台刷新中）")
                        break
                    }
                }
            }
        }
        refresh()
    }

    fun refresh(silent: Boolean = false) {
        if (refreshing) return
        refreshing = true
        viewModelScope.launch {
            try {
                if (!silent) _ui.value = _ui.value.copy(loading = true, error = null)
                val matches = Graph.live.matches(force = true)
                val leagues = matches.map { it.league }.filter { it.isNotBlank() }.distinct()
                com.qiubo.optimaltv.OtvLog.i("matches 加载成功 ${matches.size} 场")
                lastRefreshAt = System.currentTimeMillis()
                _ui.value = LiveUiState(loading = false, matches = matches, leagues = leagues)
            } catch (e: Exception) {
                com.qiubo.optimaltv.OtvLog.e("matches 加载失败: ${e.message}")
                // 静默刷新失败保留旧列表不打断页面；仅首次加载才落错误态
                if (!silent && _ui.value.matches.isEmpty()) {
                    _ui.value = _ui.value.copy(loading = false, error = e.message ?: "加载失败")
                } else if (!silent) {
                    _ui.value = _ui.value.copy(loading = false)
                }
            } finally {
                refreshing = false
            }
        }
    }

    /** 需求①：页面驻留期轮询入口——距上次成功刷新超过 60s 才真正拉取 */
    fun refreshIfStale() {
        if (System.currentTimeMillis() - lastRefreshAt >= 9_500) refresh(silent = true)
    }
    fun projectClock(now: Long = System.currentTimeMillis()) {
        val old = _ui.value
        val next = old.matches.map { MatchClockPolicy.project(it, matchStartMillis(it), now) }
        if (next != old.matches) _ui.value = old.copy(matches = next)
    }

    /** hero 榜（原版 renderLiveHero：今日/明日优先，重要度排序，前 6） */
    fun heroList(all: List<MatchItem>): List<MatchItem> {
        val zone = java.util.TimeZone.getTimeZone("Asia/Shanghai")
        val fmt = SimpleDateFormat("MM-dd", Locale.CHINA).apply { timeZone = zone }
        val today = fmt.format(Date())
        val cal = java.util.Calendar.getInstance(zone); cal.add(java.util.Calendar.DAY_OF_MONTH, 1)
        val tomorrow = fmt.format(cal.time)
        fun isMyTeam(m: MatchItem) = m.home.contains("国际米兰") || m.away.contains("国际米兰")
        val visible = visibleLiveScheduleMatches(all)
        val tt = visible.filter { matchDateMonthDay(it.date) == today || matchDateMonthDay(it.date) == tomorrow }
            .sortedWith(compareByDescending<MatchItem> { if (isMyTeam(it)) 1 else 0 }.thenByDescending { LiveViewModelImportance.of(it) })
        val others = visible.filter { matchDateMonthDay(it.date) != today && matchDateMonthDay(it.date) != tomorrow }
            .sortedWith(compareByDescending<MatchItem> { if (isMyTeam(it)) 1 else 0 }.thenByDescending { LiveViewModelImportance.of(it) })
        return (tt + others).take(6)
    }
}

/** 重要度（原版 heroImportance：live+1000 有比分+100 五大联赛/中超+50 欧战国国赛+40） */
private object LiveViewModelImportance {
    private val top5 = Regex("^(英超|西甲|意甲|德甲|法甲|中超|足协杯)")
    private val nat = Regex("欧冠|欧联|欧协|欧国联|世预赛|欧预赛|欧洲杯|亚洲杯|美洲杯|世界杯|友谊赛|国家队")
    fun of(m: MatchItem): Int {
        var s = 0
        if (m.isLive) s += 1000
        if (m.hasScore) s += 100
        if (top5.containsMatchIn(m.league)) s += 50
        if (nat.containsMatchIn(m.league)) s += 40
        return s
    }
}

/* ---------------- Screen（v1.18 布局与 TV 版 1:1 同款，交互触控） ---------------- */

/** hero 海报带高度（并入 LazyColumn 首项，随内容滚动，不再固定顶部）；
 *  需求①（2026-09-12）：竖屏自适应缩小 700→520（与影视页 VOD_BAND_PORTRAIT 同做法） */
private const val LIVE_BAND = 700f
private const val LIVE_BAND_PORTRAIT = 520f

@Composable
fun LiveScreen(nav: NavController, vm: LiveViewModel = viewModel()) {
    val ui by vm.ui.collectAsStateWithLifecycle()
    val refreshEpoch by com.qiubo.optimaltv.lifecycle.AppLifecycleManager.refreshEpoch.collectAsStateWithLifecycle()
    val s = rememberUiScale()
    LaunchedEffect(refreshEpoch) {
        if (refreshEpoch > 0L) vm.refresh(silent = true)
    }
    // 竖屏（2026-09-07 竖屏优化）：hero 内容列/CTA/分组标题/筛选行侧距按 750 基准收窄
    val portrait = androidx.compose.ui.platform.LocalConfiguration.current.orientation ==
        android.content.res.Configuration.ORIENTATION_PORTRAIT
    // v1.19 竖屏自适应：比赛卡列数（横屏公式自然=5）
    val liveCols = com.qiubo.optimaltv.ui.theme.rememberRowColumns(cardW = 320f, gap = 36f, sidePad = 86f)

    // hero 状态：仅手动切换（触控=横滑/点圆点，对应 TV 版播放钮左右键语义，不自动轮播）；
    // 按 matchId 记忆（列表刷新重排时海报不再自动换比赛，需求 #11）
    var heroId by rememberSaveable { mutableStateOf<String?>(null) }
    val heroes = remember(ui.matches) { vm.heroList(ui.matches) }
    val heroIdx = heroes.indexOfFirst { it.matchId == heroId }.takeIf { it >= 0 } ?: 0

    val listState = rememberLazyListState()
    val backScope = androidx.compose.runtime.rememberCoroutineScope()

    // 底部轻提示（需求 足球#5：半透明灰底纯文字，替代系统 Toast）
    var hint by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(hint) {
        if (hint != null) {
            delay(2500)
            hint = null
        }
    }

    // 需求①：比赛信息实时刷新——页面在前台期间每 20s 检查一次，距上次成功
    // 刷新 >60s 即静默拉取（开赛/比分变化约 1 分钟内上屏）；离开本页自动停止
    LaunchedEffect(Unit) {
        while (true) {
            delay(1_000)
            vm.projectClock()
            vm.refreshIfStale()
        }
    }

    // 需求⑥ + 2026-09-19：已在顶部 → 双击退出（首次轻提示）；内容区返回先滚回顶部
    val exitApp = com.qiubo.optimaltv.ui.components.rememberDoubleBackExit { hint = it }
    BackHandler {
        val atTop = listState.firstVisibleItemIndex == 0 && listState.firstVisibleItemScrollOffset == 0
        if (atTop) exitApp() else backScope.launch { listState.scrollToItem(0) }
    }
    // 顶栏下潜：内容离开绝对顶部即隐藏、回顶显示
    val chromeGone by remember(listState) {
        derivedStateOf {
            isScrolledFromTop(
                listState.firstVisibleItemIndex,
                listState.firstVisibleItemScrollOffset,
            )
        }
    }

    fun playMatch(m: MatchItem) {
        // 需求 足球#4：开赛前 10 分钟内才允许进入直播，其余情况提示；已完场提示结束
        if (m.isFinished) {
            hint = "比赛已结束"
            return
        }
        if (m.isClockHeld) {
            hint = m.displayStatus
            return
        }
        if (!m.isLive) {
            val start = matchStartMillis(m)
            if (start != null && System.currentTimeMillis() < start - 10 * 60_000L) {
                hint = "比赛未开始"
                return
            }
        }
        nav.navigate("player/" + URLEncoder.encode("live:" + m.matchId, "UTF-8") + "/0")
    }

    var leagueFilter by rememberSaveable { mutableStateOf("all") }

    // v1.19 流畅度：过滤+分组提级 remember——旧版在 LazyColumn content lambda 里逐次
    // 重算（LazyListScope 非组合域不能 remember），本页任何状态变化都全量重跑
    val groups = remember(ui.matches, leagueFilter) {
        val filtered = visibleLiveScheduleMatches(ui.matches, leagueFilter)
        buildMap<String, List<MatchItem>> {
            val zone = java.util.TimeZone.getTimeZone("Asia/Shanghai")
            val fmt = SimpleDateFormat("MM-dd", Locale.CHINA).apply { timeZone = zone }
            put(fmt.format(Date()), emptyList<MatchItem>())
            val cal = java.util.Calendar.getInstance(zone); cal.add(java.util.Calendar.DAY_OF_MONTH, 1)
            put(fmt.format(cal.time), emptyList<MatchItem>())
            filtered.forEach { m -> put(m.date.ifBlank { "未知" }, (get(m.date.ifBlank { "未知" }) ?: emptyList()) + m) }
        }
    }

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        when {
            ui.loading && ui.matches.isEmpty() -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                com.qiubo.optimaltv.ui.components.AppleLoading()
            }
            ui.error != null && ui.matches.isEmpty() -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text("直播数据加载失败：${ui.error}", color = OtvColors.White60, fontSize = 26f.sxs(s))
            }
            else -> {
                // 整页一个 LazyColumn：hero 海报带 → 联赛筛选 → 按日期分组比赛
                LazyColumn(
                    state = listState,
                    modifier = Modifier
                        .fillMaxSize()
                        .background(OtvColors.Bg),
                ) {
                    item(key = "hero") {
                        LiveHeroBand(
                            hero = heroes.getOrNull(heroIdx),
                            heroes = heroes,
                            heroIdx = heroIdx,
                            s = s,
                            portrait = portrait,
                            onPlay = ::playMatch,
                            onSelect = { m -> heroId = m.matchId },
                        )
                    }
                    item(key = "filters") {
                        LeagueFilterBar(
                            selected = leagueFilter,
                            onSelect = { leagueFilter = it },
                            s = s,
                            portrait = portrait,
                        )
                    }
                    // 比赛分组结果见上方提级 remember 的 groups（v1.19 流畅度）
                    var groupIdx = 0
                    groups.forEach { (date, ms) ->
                        val isFirstGroup = groupIdx++ == 0
                        item(key = "date-$date") {
                            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(start = (if (portrait) 40f else 90f).sx(s), top = 40f.sx(s))) {
                                Text(dateLabel(date), color = OtvColors.White, fontSize = 38f.sxs(s), fontWeight = FontWeight.Bold)
                                if (ms.isNotEmpty()) {
                                    Spacer(Modifier.width(18f.sx(s)))
                                    Text("${ms.size} 场", color = OtvColors.White50, fontSize = 23f.sxs(s))
                                }
                            }
                        }
                        item(key = "row-$date-h") {
                            if (ms.isEmpty()) {
                                Text(
                                    "暂无比赛", color = OtvColors.White50, fontSize = 26f.sxs(s),
                                    modifier = Modifier.padding(start = (if (portrait) 40f else 90f).sx(s), top = 20f.sx(s), bottom = 20f.sx(s)),
                                )
                            }
                        }
                        if (ms.isNotEmpty() && isFirstGroup) {
                            // 需求⑦：今日比赛横排；2026-09-05 需求④：超一屏宽折两行（均分、
                            // 保持时间顺序），未超出才单行；两行仍超宽时整体横向滚动
                            item(key = "row-$date") {
                                val pages = TodayMatchPager.pages(ms)
                                LazyRow(horizontalArrangement = Arrangement.spacedBy(40f.sx(s)),
                                    contentPadding = PaddingValues(horizontal = (if (portrait) 40f else 86f).sx(s)),
                                    modifier = Modifier.padding(top = 24f.sx(s), bottom = 40f.sx(s))) {
                                    items(pages.size) { pi ->
                                        Column(verticalArrangement = Arrangement.spacedBy(28f.sx(s))) {
                                            pages[pi].rows.forEach { row ->
                                                Row(horizontalArrangement = Arrangement.spacedBy(40f.sx(s))) {
                                                    row.forEach { GameCard(it, s, onPlay = ::playMatch) }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        } else if (ms.isNotEmpty()) {
                            // 明日及以后：每行 5 卡（320×5+4×36≈1744 ≤ 横屏可用宽）；
                            // v1.19 竖屏自适应（总体#2）：按屏宽算列数（横屏公式自然=5）。
                            // 逐行独立 item（低端机滚动性能）；间距 1:1 保留
                            ms.chunked(liveCols).forEachIndexed { ri, rowMs ->
                                item(key = "row-$date-$ri") {
                                    Row(
                                        horizontalArrangement = Arrangement.spacedBy(36f.sx(s)),
                                        modifier = Modifier
                                            .padding(top = if (ri == 0) 24f.sx(s) else 28f.sx(s))
                                            .padding(horizontal = (if (portrait) 40f else 86f).sx(s)),
                                    ) {
                                        rowMs.forEach { m ->
                                            // 竖屏 2 列卡宽自适应（weight 均分；卡内布局/高度不变，
                                            // 固定 320×2+36 在 750 基准下溢出屏宽）
                                            GameCard(m, s, if (portrait) Modifier.weight(1f) else Modifier, ::playMatch)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    item(key = "bottom-pad") { Spacer(Modifier.height(80f.sx(s))) }
                }
            }
        }
        // TabBar 悬浮在最顶层（触控版：滚动下潜）
        MainTabBar(
            "live", { key -> navigateToTab(nav, key) },
            { nav.navigate("search") },
            hidden = chromeGone,
        )
        OtvHint(hint)
    }
}

/** 联赛筛选行（原版 league-filters chips；active = 白50%底黑字，触控点按切换）；
 *  需求②（2026-09-12）：竖屏禁止横向滑动——FlowRow 换行全量展示（10 项两行内放完） */
@OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
@Composable
private fun LeagueFilterBar(
    selected: String,
    onSelect: (String) -> Unit,
    s: Float,
    portrait: Boolean = false,
) {
    @Composable
    fun LeagueChip(key: String, label: String) {
        val active = selected == key
        Box(
            Modifier
                .clip(RoundedCornerShape((if (portrait) 28f else 32f).sx(s)))
                .tapCard { onSelect(key) }
                .background(if (active) OtvColors.White.copy(alpha = 0.5f) else OtvColors.ChipBg, RoundedCornerShape((if (portrait) 28f else 32f).sx(s)))
                .padding(horizontal = (if (portrait) 24f else 30f).sx(s), vertical = (if (portrait) 12f else 14f).sx(s)),
        ) {
            Text(label, color = if (active) OtvColors.Bg else OtvColors.White60, fontSize = (if (portrait) 24f else 29f).sxs(s))
        }
    }
    if (portrait) {
        androidx.compose.foundation.layout.FlowRow(
            horizontalArrangement = Arrangement.spacedBy(10f.sx(s)),
            verticalArrangement = Arrangement.spacedBy(10f.sx(s)),
            modifier = Modifier
                .fillMaxWidth()
                .padding(top = 24f.sx(s))
                .padding(horizontal = 40f.sx(s)),
        ) {
            LEAGUE_FILTERS.forEach { (key, label) -> LeagueChip(key, label) }
        }
    } else {
        LazyRow(
            horizontalArrangement = Arrangement.spacedBy(6f.sx(s)),
            contentPadding = PaddingValues(start = 90f.sx(s), end = 50f.sx(s)),
            modifier = Modifier.padding(top = 24f.sx(s)),
        ) {
            items(LEAGUE_FILTERS.size) { i ->
                val (key, label) = LEAGUE_FILTERS[i]
                LeagueChip(key, label)
            }
        }
    }
}

/**
 * hero 海报带：海报顶到屏幕最顶 + 半透明白雾；带高 LIVE_BAND；
 * 海报=启动时随机固定一张（需求 足球#2：左右切比赛不换海报，仅每次启动 app 时更换）。
 * 触控：横滑切比赛（对应 TV 版播放钮左右键语义）；圆点可点直达。
 */
@Composable
private fun LiveHeroBand(
    hero: MatchItem?,
    heroes: List<MatchItem>,
    heroIdx: Int,
    s: Float,
    portrait: Boolean = false,
    onPlay: (MatchItem) -> Unit,
    onSelect: (MatchItem) -> Unit,
) {
    val posterIdx = Graph.live.launchPosterIndex
    val heroCtx = androidx.compose.ui.platform.LocalContext.current
    // 横滑切比赛：拖动累计超阈值（屏宽 8%）判定一次切换
    val dragAcc = remember { mutableFloatStateOf(0f) }
    // 需求①（2026-09-12）：竖屏带高 520；内容列/CTA 依窄带重排（top 120 / 390）
    val bandH = if (portrait) LIVE_BAND_PORTRAIT else LIVE_BAND
    val contentTop = if (portrait) 120f else 170f
    val ctaTop = if (portrait) 390f else 500f
    Box(
        Modifier
            .fillMaxWidth()
            .height(bandH.sx(s))
            .pointerInput(heroes.size, hero?.matchId) {
                detectHorizontalDragGestures(
                    onDragStart = { dragAcc.floatValue = 0f },
                    onDragEnd = {
                        val d = dragAcc.floatValue
                        if (heroes.isNotEmpty() && kotlin.math.abs(d) > size.width * 0.08f) {
                            val idx = heroes.indexOfFirst { it.matchId == hero?.matchId }.takeIf { it >= 0 } ?: 0
                            val next = if (d < 0) (idx + 1) % heroes.size else (idx - 1 + heroes.size) % heroes.size
                            onSelect(heroes[next])
                        }
                        dragAcc.floatValue = 0f
                    },
                ) { change, amt ->
                    change.consume()
                    dragAcc.floatValue += amt
                }
            },
    ) {
        AsyncImage(
            // v1.10：全局放开硬件位图后，仅 hero 大图按请求走软件解码
            //（超宽海报在个别 GPU 硬件位图解码失败 = 图空白，v1.4 实测）
            model = coil.request.ImageRequest.Builder(heroCtx)
                .data(Graph.live.posterUrl(posterIdx))
                .allowHardware(false)
                .build(),
            contentDescription = null,
            contentScale = ContentScale.Crop,
            // 需求 足球#3：海报加载失败落日志（黑屏观感可能是海报没出+背景全黑）
            onState = { st ->
                if (st is coil.compose.AsyncImagePainter.State.Error) {
                    com.qiubo.optimaltv.OtvLog.w("live hero poster 加载失败: ${st.result.throwable?.message}")
                }
            },
            modifier = Modifier.fillMaxSize(),
        )
        // 半透明白雾
        Box(Modifier.fillMaxSize().background(Color.White.copy(alpha = 0.10f)))
        // 左侧压暗（保证比分文字可读）
        Box(
            Modifier
                .fillMaxSize()
                .background(Brush.horizontalGradient(listOf(Color(0x38000000), Color(0x14000000), Color.Transparent))),
        )
        // 底部渐黑：与下方内容区（实底 Bg）自然衔接
        Box(
            Modifier
                .align(Alignment.BottomCenter)
                .fillMaxWidth()
                .height(160f.sx(s))
                .background(Brush.verticalGradient(listOf(Color(0x00000000), OtvColors.Bg))),
        )
        if (hero != null) {
            // 需求⑦（2026-09-07）：左右滑动切换比赛时比分/状态内容淡入+轻位移
            androidx.compose.animation.AnimatedContent(
                targetState = hero,
                modifier = Modifier.align(Alignment.TopStart),
                transitionSpec = {
                    (androidx.compose.animation.fadeIn(androidx.compose.animation.core.tween(360)) +
                        androidx.compose.animation.slideInVertically(androidx.compose.animation.core.tween(360)) { it / 8 })
                        .togetherWith(androidx.compose.animation.fadeOut(androidx.compose.animation.core.tween(200)))
                },
                label = "liveHeroContent",
            ) { animatedHero ->
                Column(
                    Modifier
                        .padding(start = (if (portrait) 40f else 80f).sx(s), top = contentTop.sx(s))
                        .width((if (portrait) 640f else 760f).sx(s)),
                ) {
                ScoreRow(animatedHero.home, animatedHero.homeScore, s)
                ScoreRow(animatedHero.away, animatedHero.awayScore, s)
                Spacer(Modifier.height(24f.sx(s)))
                // sb-meta（需求 #4）：直播中 = LIVE 徽章 + 联赛 + 分钟（「LIVE 西甲 35」）；
                // 未开赛/完场 = 状态 + 联赛 月日 时间
                Row(verticalAlignment = Alignment.CenterVertically) {
                    if (animatedHero.isLive) {
                        LiveBadge(s)
                        Spacer(Modifier.width(14f.sx(s)))
                        Text(
                            listOf(
                                animatedHero.league,
                                if (animatedHero.minute.isNotBlank()) animatedHero.minute + "'" else animatedHero.time,
                            )
                                .filter { it.isNotBlank() }
                                .joinToString("  "),
                            color = OtvColors.White, fontSize = 30f.sxs(s), fontWeight = FontWeight.SemiBold,
                        )
                    } else {
                        Text(
                            animatedHero.displayStatus,
                            color = OtvColors.White.copy(alpha = 0.85f), fontSize = 28f.sxs(s), fontWeight = FontWeight.SemiBold,
                        )
                        Spacer(Modifier.width(14f.sx(s)))
                        Text(
                            matchTimeLabel(animatedHero),
                            color = OtvColors.White, fontSize = 28f.sxs(s), fontWeight = FontWeight.SemiBold,
                        )
                    }
                }
                }
            }
            // cta：btn-play 纯白底 268×94 r12 + 实心播放三角（触控点按进直播）；竖屏 top 390（520 带内）
            Box(
                Modifier
                    .align(Alignment.TopStart)
                    .padding(start = (if (portrait) 40f else 80f).sx(s), top = ctaTop.sx(s)),
            ) {
                Box(
                    Modifier
                        .clip(RoundedCornerShape(12f.sx(s)))
                        .tapCard { onPlay(hero) }
                        .background(OtvColors.White, RoundedCornerShape(12f.sx(s)))
                        .width(268f.sx(s))
                        .height(94f.sx(s)),
                    contentAlignment = Alignment.Center,
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Canvas(Modifier.size(34f.sx(s))) {
                            val p = Path().apply {
                                moveTo(0f, 0f); lineTo(size.width, size.height / 2f); lineTo(0f, size.height); close()
                            }
                            drawPath(p, OtvColors.Bg)
                        }
                        Spacer(Modifier.width(14f.sx(s)))
                        Text("立即观看", color = OtvColors.Bg, fontSize = 29f.sxs(s), fontWeight = FontWeight.Medium)
                    }
                }
            }
        }
        // hero-dots（带内底部右端，胶囊底；触控可点直达指定比赛）
        if (heroes.size > 1) {
            Row(
                horizontalArrangement = Arrangement.spacedBy(10f.sx(s)),
                modifier = Modifier
                    .align(Alignment.BottomEnd)
                    .padding(end = 40f.sx(s), bottom = 30f.sx(s))
                    .background(OtvColors.TabBarBg, RoundedCornerShape(26f.sx(s)))
                    .padding(horizontal = 12f.sx(s), vertical = 8f.sx(s)),
            ) {
                heroes.forEachIndexed { i, m ->
                    Box(
                        Modifier
                            .clip(CircleShape)
                            .tapCard { onSelect(m) }
                            .padding(3f.sx(s))
                            .size(10f.sx(s))
                            .background(if (i == heroIdx) OtvColors.White else OtvColors.White30, CircleShape),
                    )
                }
            }
        }
    }
}

/** LIVE 徽章（需求 #3/#4：红底胶囊白字粗体，内边距撑起高度 → 文字天然上下居中） */
@Composable
private fun LiveBadge(s: Float) {
    Box(
        Modifier
            .background(OtvColors.Live, RoundedCornerShape(17f.sx(s)))
            .padding(horizontal = 14f.sx(s), vertical = 3f.sx(s)),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            "LIVE", color = OtvColors.White, fontSize = 22f.sxs(s), fontWeight = FontWeight.Bold,
            lineHeight = 26f.sxs(s),
        )
    }
}

/** sb-row（原版 hero scoreboard 行：队徽84 + 队名动态76/60/50/42 + 比分76右对齐） */
@Composable
private fun ScoreRow(team: String, pts: String, s: Float) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.height(96f.sx(s)),
    ) {
        Box(
            Modifier.size(84f.sx(s)),
            contentAlignment = Alignment.Center,
        ) {
            // 2026-09-18 队标重叠修复：首字兜底只在图标未就绪时绘制。透明底队徽（尤文图斯
            // 等 SVG）会把压在下面的白字切成碎片，看上去就是"队标重叠"。
            var iconReady by remember(team) { mutableStateOf(false) }
            if (!iconReady) {
                Text(team.take(1), color = OtvColors.White, fontSize = 40f.sxs(s), fontWeight = FontWeight.Bold)
            }
            coil.compose.AsyncImage(
                model = Graph.live.teamIconUrl(team),
                contentDescription = team,
                contentScale = ContentScale.Fit,
                onSuccess = { state ->
                    // v1.32：后端 1×1 占位像素不算就绪（保持首字兜底）——旧版占位像素实为
                    // 半透明黄色且此处无条件置 ready，1px 被 Fit 拉伸成整格「黄色方块」。
                    // intrinsicWidth=-1（SVG 无固有尺寸）按就绪处理，仅 1..2px 判占位。
                    val w = state.result.drawable.intrinsicWidth
                    iconReady = w !in 1..2
                },
                onError = { iconReady = false },
                modifier = Modifier.fillMaxSize().padding(6f.sx(s)),
            )
        }
        Spacer(Modifier.width(16f.sx(s)))
        val nameSize = (if (team.length > 10) 42 else if (team.length > 8) 50 else if (team.length > 6) 60 else 76).toFloat()
        Text(team, color = OtvColors.White, fontSize = nameSize.sxs(s), fontWeight = FontWeight.Bold, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        if (pts.isNotBlank()) {
            Spacer(Modifier.width(12f.sx(s)))
            Text(pts, color = OtvColors.White, fontSize = 76f.sxs(s), fontWeight = FontWeight.Bold)
        }
    }
}

/** 固定分类集（全部/重要/五大/中超/欧战/国家队）。v1.16：新增中超（用户需求） */
val LEAGUE_FILTERS = listOf(
    "all" to "全部", "important" to "重要",
    "英超" to "英超", "西甲" to "西甲", "意甲" to "意甲", "德甲" to "德甲", "法甲" to "法甲",
    "中超" to "中超",
    "euro" to "欧战", "national" to "国家队",
)

private val TOP5_RE = Regex("^(英超|西甲|意甲|德甲|法甲|中超|足协杯)")
private val EURO_RE = Regex("欧冠|欧联|欧协|欧罗巴|欧国联|欧会杯")
private val NAT_RE = Regex("世预赛|欧预赛|欧洲杯|亚洲杯|美洲杯|世界杯|友谊赛|国家队|国联")

/** 原版 filterLeagues 语义：important=五大+欧战国国赛；欧战/国家队为归并类，具体联赛精确匹配 */
fun matchLeagueFilter(m: MatchItem, key: String): Boolean = when (key) {
    "all" -> true
    "important" -> ImportantTeamPolicy.isImportant(m)
    "euro" -> EURO_RE.containsMatchIn(m.league)
    "national" -> NAT_RE.containsMatchIn(m.league)
    // v1.16：中超 chip = 中超+足协杯（都是中超球队的比赛）；其余具体联赛用 contains
    "中超" -> m.league.contains("中超") || m.league.contains("足协杯")
    else -> m.league.contains(key)
}

/** 直播赛程列表只显示未开赛或进行中的赛事；完场赛事留给战报页。 */
fun visibleLiveScheduleMatches(matches: List<MatchItem>, leagueFilter: String = "all"): List<MatchItem> =
    matches.filter { !it.isFinished && matchLeagueFilter(it, leagueFilter) }

private val MATCH_DATE_FULL_RE = Regex("^(\\d{4})-(\\d{2})-(\\d{2})$")
private val MATCH_DATE_SHORT_RE = Regex("^(\\d{2})-(\\d{2})$")

fun matchDateMonthDay(value: String): String {
    val full = MATCH_DATE_FULL_RE.find(value.trim())
    if (full != null) return "${full.groupValues[2]}-${full.groupValues[3]}"
    return MATCH_DATE_SHORT_RE.find(value.trim())?.value.orEmpty()
}

private fun matchDateDisplay(value: String): String {
    val full = MATCH_DATE_FULL_RE.find(value.trim())
    val monthDay = full?.let { "${it.groupValues[2]}-${it.groupValues[3]}" } ?: value.trim()
    val short = MATCH_DATE_SHORT_RE.find(monthDay) ?: return ""
    return "${short.groupValues[1].toInt()}月${short.groupValues[2].toInt()}日"
}

fun dateLabel(date: String): String {
    val zone = java.util.TimeZone.getTimeZone("Asia/Shanghai")
    val fmt = SimpleDateFormat("MM-dd", Locale.CHINA).apply { timeZone = zone }
    val today = fmt.format(Date())
    val cal = java.util.Calendar.getInstance(zone); cal.add(java.util.Calendar.DAY_OF_MONTH, 1)
    val tomorrow = fmt.format(cal.time)
    return when (matchDateMonthDay(date)) {
        today -> "今日比赛"; tomorrow -> "明日比赛"
        else -> matchDateDisplay(date).takeIf(String::isNotBlank)?.plus(" 比赛") ?: "比赛"
    }
}

/** 比赛卡（TV 版 1:1：黑80%圆角卡 320×146 + 左右队块 + 中部联赛/LIVE/比分/VS；触控点按进直播）
 *  modifier 传入 RowScope.weight(1f) 时竖屏均分自适应宽（卡高/内部样式不变） */
@Composable
private fun GameCard(
    m: MatchItem,
    s: Float,
    modifier: Modifier = Modifier,
    onPlay: (MatchItem) -> Unit = {},
) {
    Column(modifier.width(320f.sx(s))) {
        Box(
            Modifier
                .clip(RoundedCornerShape(16f.sx(s)))
                .tapCard { onPlay(m) }
                .background(OtvColors.CardBlack80, RoundedCornerShape(16f.sx(s)))
                .fillMaxWidth()
                .height(146f.sx(s))
                .padding(horizontal = 16f.sx(s)),
            contentAlignment = Alignment.Center,
        ) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
                TeamBlock(m.home, s)
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    // 需求 2026-09-11：联赛/VS/时间三行上下收紧——行距 5→2，各文本显式紧凑行高
                    //（不设行高时中文联赛名按 CJK 字体度量撑高，三行整体偏松）
                    verticalArrangement = Arrangement.spacedBy(2f.sx(s)),
                ) {
                    // 需求 足球#6：卡内中部显示联赛/杯赛名（意甲、意大利杯等）
                    if (m.league.isNotBlank()) {
                        Text(
                            m.league, color = OtvColors.White60, fontSize = 20f.sxs(s), lineHeight = 22f.sxs(s),
                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                        )
                    }
                    if (m.isLive) {
                        LiveBadge(s)
                        if (m.minute.isNotBlank()) {
                            Text(m.minute + "'", color = OtvColors.White, fontSize = 26f.sxs(s), lineHeight = 28f.sxs(s), fontWeight = FontWeight.SemiBold)
                        }
                        if (m.hasScore) {
                            Text(
                                "${m.homeScore} - ${m.awayScore}",
                                color = OtvColors.White, fontSize = 34f.sxs(s), lineHeight = 36f.sxs(s), fontWeight = FontWeight.Bold,
                            )
                        }
                    } else if (m.isFinished) {
                        if (m.hasScore) {
                            Text(
                                "${m.homeScore} - ${m.awayScore}",
                                color = OtvColors.White, fontSize = 34f.sxs(s), lineHeight = 36f.sxs(s), fontWeight = FontWeight.Bold,
                            )
                        } else {
                            Text("完场", color = OtvColors.White60, fontSize = 30f.sxs(s), lineHeight = 32f.sxs(s), fontWeight = FontWeight.Bold)
                        }
                    } else if (m.isClockHeld) {
                        Text(m.displayStatus, color = OtvColors.White60, fontSize = 30f.sxs(s), lineHeight = 32f.sxs(s), fontWeight = FontWeight.Bold)
                    } else if (m.hasScore) {
                        Text(
                            "${m.homeScore} - ${m.awayScore}",
                            color = OtvColors.White, fontSize = 34f.sxs(s), lineHeight = 36f.sxs(s), fontWeight = FontWeight.Bold,
                        )
                    } else {
                        Text("VS", color = OtvColors.White60, fontSize = 30f.sxs(s), lineHeight = 32f.sxs(s), fontWeight = FontWeight.Bold)
                    }
                    // 2026-09-05：未开赛/完场卡片显示开赛时间（直播中已有 LIVE 徽章/分钟数）
                    if (!m.isLive && m.time.isNotBlank()) {
                        Text(m.time, color = OtvColors.White50, fontSize = 20f.sxs(s), lineHeight = 22f.sxs(s), fontWeight = FontWeight.Medium)
                    }
                }
                TeamBlock(m.away, s)
            }
        }
    }
}

/** 未开赛时间文案 = 「联赛 8月12日 7:00」；直播中 = 分钟数（卡片内已不用，hero 保留） */
fun matchTimeLabel(m: MatchItem): String {
    if (m.isLive) return if (m.minute.isNotBlank()) "${m.minute}'" else m.time
    val t = m.time.ifBlank { "" }
    val league = m.league.ifBlank { "" }
    val dateLabel = matchDateDisplay(m.date)
    return listOf(league, dateLabel, t).filter { it.isNotBlank() }.joinToString(" ")
}

/** 按北京时间解析开赛时刻；完整日期来自后端，旧 MM-dd 快照仅在跨年邻近窗口内纠正年份。 */
fun matchStartMillis(m: MatchItem, nowMillis: Long = System.currentTimeMillis()): Long? {
    val exactDate = m.dateFull.ifBlank { m.date }.trim()
    val fullDate = MATCH_DATE_FULL_RE.find(exactDate)
    val shortDate = MATCH_DATE_SHORT_RE.find(exactDate)
    if (fullDate == null && shortDate == null) return null
    val tm = Regex("^(\\d{1,2}):(\\d{2})").find(m.time.trim()) ?: return null
    val zone = java.util.TimeZone.getTimeZone("Asia/Shanghai")
    fun makeMillis(year: Int, month: Int, day: Int): Long? = runCatching {
        java.util.Calendar.getInstance(zone, Locale.CHINA).apply {
            clear()
            set(year, month - 1, day, tm.groupValues[1].toInt(), tm.groupValues[2].toInt(), 0)
        }.timeInMillis
    }.getOrNull()
    if (fullDate != null) {
        return makeMillis(fullDate.groupValues[1].toInt(), fullDate.groupValues[2].toInt(), fullDate.groupValues[3].toInt())
    }
    val month = shortDate!!.groupValues[1].toInt()
    val day = shortDate.groupValues[2].toInt()
    val nowCal = java.util.Calendar.getInstance(zone, Locale.CHINA).apply { timeInMillis = nowMillis }
    val currentYear = nowCal.get(java.util.Calendar.YEAR)
    val candidates = (currentYear - 1..currentYear + 1).mapNotNull { makeMillis(it, month, day) }
    val nearby = candidates.filter { kotlin.math.abs(it - nowMillis) <= 31L * 24 * 60 * 60 * 1000 }
    return nearby.minByOrNull { kotlin.math.abs(it - nowMillis) } ?: makeMillis(currentYear, month, day)
}

/** 队标+队名（原版 gc-team-wrap：72 队标 + 动态队名，加载失败首字兜底）
 *  需求 足球#6：一行放得下就优先只放一行——字号按列宽/字数自适应收缩，超 8 字才折行 */
@Composable
private fun TeamBlock(team: String, s: Float) {
    Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.width(96f.sx(s))) {
        Box(
            Modifier.size(72f.sx(s)),
            contentAlignment = Alignment.Center,
        ) {
            // 2026-09-18 队标重叠修复：同 TeamIcon 行，兜底首字只在未加载出队徽时显示
            var iconReady by remember(team) { mutableStateOf(false) }
            if (!iconReady) {
                Text(team.take(1), color = OtvColors.White, fontSize = 34f.sxs(s), fontWeight = FontWeight.Bold)
            }
            coil.compose.AsyncImage(
                model = Graph.live.teamIconUrl(team),
                contentDescription = team,
                contentScale = ContentScale.Fit,
                onSuccess = { state ->
                    // v1.32：后端 1×1 占位像素不算就绪（保持首字兜底）——旧版占位像素实为
                    // 半透明黄色且此处无条件置 ready，1px 被 Fit 拉伸成整格「黄色方块」。
                    // intrinsicWidth=-1（SVG 无固有尺寸）按就绪处理，仅 1..2px 判占位。
                    val w = state.result.drawable.intrinsicWidth
                    iconReady = w !in 1..2
                },
                onError = { iconReady = false },
                modifier = Modifier.fillMaxSize().padding(6f.sx(s)),
            )
        }
        Spacer(Modifier.height(4f.sx(s)))
        val wrap = team.length > 8
        // 单行可容纳字号 ≈ 列宽/字数（留 4px 余量）；上限 18，保底 11（再长折行）
        val fitSize = (92f / team.length).coerceIn(11f, 18f)
        Text(
            team, color = OtvColors.White, textAlign = TextAlign.Center,
            maxLines = if (wrap) 2 else 1,
            overflow = if (wrap) TextOverflow.Ellipsis else TextOverflow.Clip,
            lineHeight = (if (wrap) 17f else fitSize).sxs(s),
            fontSize = (if (wrap) 14f else fitSize).sxs(s),
        )
    }
}

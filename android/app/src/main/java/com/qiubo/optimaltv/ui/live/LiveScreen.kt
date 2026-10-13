package com.qiubo.optimaltv.ui.live

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import androidx.compose.animation.togetherWith   // 需求⑦：hero 切换动画 transitionSpec 用
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.model.MatchItem
import com.qiubo.optimaltv.ui.components.FocusRegistry
import com.qiubo.optimaltv.ui.components.InitialFocusEffect
import com.qiubo.optimaltv.ui.components.NAV_DOWN
import com.qiubo.optimaltv.ui.components.NAV_LEFT
import com.qiubo.optimaltv.ui.components.NAV_RIGHT
import com.qiubo.optimaltv.ui.components.NAV_UP
import com.qiubo.optimaltv.ui.components.MainTabBar
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.ProvideNavScrolls
import com.qiubo.optimaltv.ui.components.ReturnFocus
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.components.lazyNavContainer
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

/* ---------------- VM ---------------- */

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

internal fun liveMatchFailureState(current: LiveUiState, message: String): LiveUiState =
    if (current.matches.isEmpty()) current.copy(loading = false, error = message)
    else current.copy(loading = false, error = null)

internal fun liveRefreshDue(lastAttemptAt: Long, now: Long): Boolean =
    lastAttemptAt <= 0L || now - lastAttemptAt >= 9_500L

class LiveViewModel : ViewModel() {
    private val _ui = MutableStateFlow(LiveUiState())
    val ui: StateFlow<LiveUiState> = _ui.asStateFlow()

    /** 上次尝试时间 / 刷新进行中标记，失败后仍隔 10 秒自动重试，避免每秒重打上游。 */
    private var lastRefreshAttemptAt = 0L
    @Volatile private var refreshing = false

    init {
        // v1.10（2026-08-31 低端机秒开）：先恢复磁盘快照立即上屏——冷启动链路
        // （后端冷抓期间）不再白屏转圈；后台 refresh 到货后替换。
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
                _ui.value = LiveUiState(loading = false, matches = matches, leagues = leagues)
            } catch (e: Exception) {
                com.qiubo.optimaltv.OtvLog.e("matches 加载失败: ${e.message}")
                // 初次请求即使由静默轮询触发，空页面也要结束 loading 并显示错误；已有数据则保留。
                _ui.value = liveMatchFailureState(_ui.value, e.message ?: "加载失败")
            } finally {
                lastRefreshAttemptAt = System.currentTimeMillis()
                refreshing = false
            }
        }
    }

    /** 前台每 10 秒静默刷新；后端同时按本机时间投影缓存状态，开球不再等待源站缓存。 */
    fun refreshIfStale() {
        if (liveRefreshDue(lastRefreshAttemptAt, System.currentTimeMillis())) refresh(silent = true)
    }

    fun projectClock(nowMillis: Long = System.currentTimeMillis()) {
        val current = _ui.value
        val projected = current.matches.map { MatchClockPolicy.project(it, matchStartMillis(it), nowMillis) }
        if (projected != current.matches) _ui.value = current.copy(matches = projected)
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

/* ---------------- Screen ---------------- */

/** hero 海报带高度（2026-08-28：并入 LazyColumn 首项，随内容滚动，不再固定顶部） */
private const val LIVE_BAND = 700f

@Composable
fun LiveScreen(nav: NavController, vm: LiveViewModel = viewModel()) {
    val ui by vm.ui.collectAsStateWithLifecycle()
    val refreshEpoch by com.qiubo.optimaltv.lifecycle.AppLifecycleManager.refreshEpoch.collectAsStateWithLifecycle()
    val s = rememberUiScale()
    LaunchedEffect(refreshEpoch) {
        if (refreshEpoch > 0L) vm.refresh(silent = true)
    }
    // 返回落焦（需求 影视#6-2）：从播放页返回时回到上次聚焦的比赛卡/hero 按钮
    val returnKey = remember { ReturnFocus.take("live") }
    val tabFocus = remember { FocusRequester() }
    val cardReg = remember { FocusRegistry() }
    InitialFocusEffect(tabFocus, "live-tab", enabled = returnKey == null)
    var restoredFocus by remember { mutableStateOf(false) }

    // hero 状态：仅手动左右键切换；按 matchId 记忆（列表刷新重排时海报不再自动换比赛，需求 #11）
    var heroId by rememberSaveable { mutableStateOf<String?>(null) }
    val heroes = remember(ui.matches) { vm.heroList(ui.matches) }
    val heroIdx = heroes.indexOfFirst { it.matchId == heroId }.takeIf { it >= 0 } ?: 0
    val heroBtnFocus = remember { FocusRequester() }

    val listState = rememberLazyListState()
    val backScope = androidx.compose.runtime.rememberCoroutineScope()
    var tabFocused by remember { mutableStateOf(false) }

    // 底部轻提示（需求 足球#5：半透明灰底纯文字，替代系统 Toast）
    var hint by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(hint) {
        if (hint != null) {
            delay(2500)
            hint = null
        }
    }

    // 页面前台每秒检查刷新条件；成功或失败后约 10 秒再拉取，离开本页自动停止。
    LaunchedEffect(Unit) {
        while (true) {
            delay(1_000)
            vm.projectClock()
            vm.refreshIfStale()
        }
    }

    // 需求⑥：在顶部 tab 栏按返回——双击才彻底退出应用，首按轻提示
    val exitApp = com.qiubo.optimaltv.ui.components.rememberDoubleBackExit { hint = it }

    fun playMatch(m: MatchItem) {
        // 需求 足球#4：开赛前 10 分钟内才允许进入直播，其余情况提示；已完场提示结束
        // v1.23：后端完场状态值是 "finished"（parse_matches），旧版误查 "ended"
        // 判定从未生效——完场卡还能点进播放页再吃「未开播」报错（状态不准观感来源之一）
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
    // 需求 足球#2：立即观看按钮 DOWN → 直达当前选中（上一次选中）的联赛筛选 chip
    val filterChipFocus = remember { FocusRequester() }
    // 筛选行聚焦状态（BACK 分层判定用）
    var filterChipsFocused by remember { mutableStateOf(false) }
    // 2026-09-05（用户反馈，同影视页）：筛选行为内容顶行、页面滚到顶时 UP 直达顶部
    // tab 栏——此前必经 hero「立即观看」中转；未到顶仍走引擎滚动找回（露出 hero）。
    // 判顶用 offset 容差（ensureVisible 会微滚 ~16px 致 canScrollBackward 误判），
    // 命中时顺手归零滚动。
    val contentUpToTab: () -> Boolean = {
        if (listState.firstVisibleItemIndex == 0 && listState.firstVisibleItemScrollOffset <= 64) {
            backScope.launch {
                runCatching { listState.scrollToItem(0) }
                repeat(10) {
                    if (runCatching { tabFocus.requestFocus() }.isSuccess) return@launch
                    delay(50)
                }
            }
            true
        } else false
    }
    // 需求 足球#5：首个比赛分组顶行卡 UP → 回「上次选中」的联赛 chip
    val upToFilterChip: () -> Unit = {
        backScope.launch {
            repeat(10) {
                if (runCatching { filterChipFocus.requestFocus() }.isSuccess) return@launch
                delay(50)
            }
        }
    }

    BackHandler {
        val atTop = listState.firstVisibleItemIndex == 0 && listState.firstVisibleItemScrollOffset == 0
        when {
            // 需求 足球#5：内容区（比赛卡/hero）BACK → 滚回顶部并落「上次选中」的联赛 chip；
            // 筛选行上 BACK → 当前页 tab；tab 上 BACK → 双击退出应用（需求⑥，首按提示）
            !tabFocused && !filterChipsFocused -> backScope.launch {
                listState.scrollToItem(0)
                repeat(10) {
                    if (runCatching { filterChipFocus.requestFocus() }.isSuccess) return@launch
                    delay(50)
                }
            }
            atTop && tabFocused -> exitApp()
            else -> {
                backScope.launch { listState.scrollToItem(0) }
                runCatching { tabFocus.requestFocus() }
            }
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
                // 返回落焦（需求 影视#6-2）：数据就绪后找回上次聚焦的比赛卡；找不回则落 tab
                LaunchedEffect(ui.matches) {
                    val key = returnKey ?: return@LaunchedEffect
                    if (restoredFocus || ui.matches.isEmpty()) return@LaunchedEffect
                    restoredFocus = true
                    // "hero" 用播放钮自带的 heroBtnFocus（registry 里没有注册该 key）
                    val target = if (key == "hero") heroBtnFocus else cardReg.fr(key)
                    repeat(40) {
                        if (runCatching { target.requestFocus() }.isSuccess) return@LaunchedEffect
                        delay(100)
                    }
                    runCatching { tabFocus.requestFocus() }
                }
                // 整页一个 LazyColumn：hero 海报带 → 联赛筛选 → 按日期分组比赛
                // v1.13：方向导航统一 OtvNav 引擎，容器仅声明滚动挂靠
                val liveScroll = remember(listState) { lazyNavContainer(listState) }
                // 比赛分组（今日/明日固定分区 + 按日期分组，原版 renderGameRow）
                // 需求 足球#8：今日 = 双行横滑（LazyRow 每列上下两卡）；明日及以后 =
                // 每行固定 5 卡、行数不限（随页面上下滚动，21 场也完整可见）
                // v1.19 流畅度：过滤+分组提级并 remember——旧版在 LazyColumn content
                // lambda 里逐次重算，本页任何状态变化（hint/tab 焦点/hero/筛选）都全量
                // 重跑过滤+建组+两次日期格式化；LazyListScope 非组合域不能 remember
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
                ProvideNavScrolls(vertical = liveScroll) {
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
                            heroBtnFocus = heroBtnFocus,
                            tabFocus = tabFocus,
                            backScope = backScope,
                            listState = listState,
                            filterChipFocus = filterChipFocus,
                            cardReg = cardReg,
                            onPlay = ::playMatch,
                            onPrev = {
                                if (heroes.isNotEmpty()) heroId = heroes[(heroIdx - 1 + heroes.size) % heroes.size].matchId
                            },
                            onNext = {
                                if (heroes.isNotEmpty()) heroId = heroes[(heroIdx + 1) % heroes.size].matchId
                            },
                        )
                    }
                    item(key = "filters") {
                        LeagueFilterBar(
                            selected = leagueFilter,
                            onSelect = { leagueFilter = it },
                            s = s,
                            selectedFocus = filterChipFocus,
                            onChipsFocused = { filterChipsFocused = it },
                            upToTab = contentUpToTab,
                        )
                    }
                    // 比赛分组结果见 LazyColumn 外的 groups（v1.19 提级 remember）
                    var groupIdx = 0
                    groups.forEach { (date, ms) ->
                        val isFirstGroup = groupIdx++ == 0
                        item(key = "date-$date") {
                            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(start = 90f.sx(s), top = 40f.sx(s))) {
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
                                    modifier = Modifier.padding(start = 90f.sx(s), top = 20f.sx(s), bottom = 20f.sx(s)),
                                )
                            }
                        }
                        if (ms.isNotEmpty() && isFirstGroup) {
                            // 需求⑦：今日比赛横排展示；2026-09-05 需求④：卡片超一屏宽时
                            // 折两行（均分，行序保持时间顺序），未超出才单行——两行仍超宽
                            // 时整体横向滚动。1920 设计宽下单行容量 = floor((1920-2×86+40)/360)=4。
                            // 每行独立 LazyRow+ProvideNavScrolls（行内滚动互不干扰，几何导航跨行照常）。
                            // 横向留白用 contentPadding（需求 足球#1）：选中环行首/行尾不裁剪。
                            item(key = "row-$date") {
                                val pages = TodayMatchPager.pages(ms)
                                val pageState = rememberLazyListState()
                                val pageScroll = remember(pageState) { lazyNavContainer(pageState) }
                                ProvideNavScrolls(horizontal = pageScroll) {
                                    LazyRow(state = pageState, contentPadding = PaddingValues(horizontal = 86f.sx(s)),
                                        horizontalArrangement = Arrangement.spacedBy(40f.sx(s)), modifier = Modifier.padding(top = 24f.sx(s), bottom = 40f.sx(s))) {
                                        items(pages.size, key = { "today-page-$date-$it" }) { pi ->
                                            Column(verticalArrangement = Arrangement.spacedBy(28f.sx(s))) {
                                                pages[pi].rows.forEachIndexed { ri, row ->
                                                    Row(horizontalArrangement = Arrangement.spacedBy(40f.sx(s))) {
                                                        row.forEachIndexed { ci, match ->
                                                            GameCard(nav, match, s, ::playMatch,
                                                                upToChip = if (pi == 0 && ri == 0 && ci == 0) upToFilterChip else null,
                                                                cardReg = cardReg)
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        } else if (ms.isNotEmpty()) {
                            // 明日及以后：每行固定 5 卡（320×5 + 4×36 ≈ 1744 ≤ 可用宽），行数不限随页滚动。
                            // v1.10（2026-08-31）：逐行独立 item（原为单个巨 item 内非 lazy Column，
                            // 比赛多时整页一次性组合全部行，低端机切页/滚动卡顿）；间距 1:1 保留
                            // （首行 top24 / 行距 28 / 组间由下一组日期标题 top40 衔接）
                            ms.chunked(5).forEachIndexed { ri, rowMs ->
                                item(key = "row-$date-$ri") {
                                    Row(
                                        horizontalArrangement = Arrangement.spacedBy(36f.sx(s)),
                                        modifier = Modifier
                                            .padding(top = if (ri == 0) 24f.sx(s) else 28f.sx(s))
                                            .padding(horizontal = 86f.sx(s)),
                                    ) {
                                        rowMs.forEach { m ->
                                            GameCard(nav, m, s, ::playMatch, cardReg = cardReg)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    item(key = "bottom-pad") { Spacer(Modifier.height(80f.sx(s))) }
                }
                } // ProvideNavScrolls(liveScroll)
            }
        }
        // TabBar 悬浮在最顶层
        MainTabBar(
            "live", { key -> com.qiubo.optimaltv.ui.components.navigateToTab(nav, key) },
            { nav.navigate("search") }, tabFocus,
            contentDownFocus = heroBtnFocus,
            contentDownScrollTop = { listState.scrollToItem(0) },
            onTabFocused = { tabFocused = it },
            autoHide = true,   // v1.16：光标下移进内容区隐藏顶栏，回顶部显示
        )
        OtvHint(hint)
    }
}

/** 联赛筛选行（原版 league-filters chips；
 *  selectedFocus 绑定在当前选中 chip 上——立即观看按钮 DOWN 直达，需求 足球#2） */
@Composable
private fun LeagueFilterBar(
    selected: String,
    onSelect: (String) -> Unit,
    s: Float,
    selectedFocus: FocusRequester? = null,
    onChipsFocused: ((Boolean) -> Unit)? = null,
    upToTab: (() -> Boolean)? = null,
) {
    val rowState = rememberLazyListState()
    val rowScroll = remember(rowState) { lazyNavContainer(rowState) }
    ProvideNavScrolls(horizontal = rowScroll) {
        LazyRow(
            state = rowState,
            horizontalArrangement = Arrangement.spacedBy(6f.sx(s)),
            // 需求 足球#1：横向留白用 contentPadding——视口扩到全屏宽，
            // 边缘 chip 聚焦 scale 外扩不再被视口裁剪
            contentPadding = PaddingValues(start = 90f.sx(s), end = 50f.sx(s)),
            modifier = Modifier.padding(top = 24f.sx(s)),
        ) {
        // 方案 §11.1：联赛筛选 chip 用稳定的联赛 key，不用 index。
        // LEAGUE_FILTERS 是编译期常量，index 当 key 看似安全，但把「位置即身份」的
        // 写法留在代码里，后续一旦改成动态列表就会静默退化成焦点漂移。
        items(LEAGUE_FILTERS.size, key = { i -> "league-${LEAGUE_FILTERS[i].first}" }) { i ->
            val (key, label) = LEAGUE_FILTERS[i]
            val active = selected == key
            var chipFocused by remember { mutableStateOf(false) }
            Box(
                Modifier
                    .then(if (active && selectedFocus != null) Modifier.focusRequester(selectedFocus) else Modifier)
                    .dpadFocusable(
                        scaleFocused = 1.05f, focusedBg = OtvColors.White,
                        // floating：筛选 chips 不参与垂直几何候选——比赛卡 UP/hero DOWN 到筛选行
                        // 都由定向逻辑直达「选中 chip」（需求 足球#5），避免 x 最近误命中并 autoSelect 切筛选
                        floating = true,
                        onFocusedChange = {
                            chipFocused = it
                            onChipsFocused?.invoke(it)
                        },
                        // 2026-09-05：页面滚到顶时 UP 直达 tab 栏（不拦截时引擎会先命中
                        // hero「立即观看」——与影视页同款 gateway 问题）
                        navOverride = { dir ->
                            if (dir == NAV_UP) (upToTab?.invoke() ?: false) else false
                        },
                        autoSelect = true,
                    ) {
                        onSelect(key)
                    }
                    .background(if (active) OtvColors.White.copy(alpha = 0.5f) else OtvColors.ChipBg, RoundedCornerShape(32f.sx(s)))
                    .padding(horizontal = 30f.sx(s), vertical = 14f.sx(s)),
            ) {
                Text(label, color = if (active || chipFocused) OtvColors.Bg else OtvColors.White60, fontSize = 29f.sxs(s))
            }
        }
        }
    }
}

/**
 * hero 海报带：海报顶到屏幕最顶 + 半透明白雾；带高 LIVE_BAND；
 * 海报=启动时随机固定一张（需求 足球#2：左右切比赛不换海报，仅每次启动 app 时更换）。
 */
@Composable
private fun LiveHeroBand(
    hero: MatchItem?,
    heroes: List<MatchItem>,
    heroIdx: Int,
    s: Float,
    heroBtnFocus: FocusRequester,
    tabFocus: FocusRequester,
    backScope: kotlinx.coroutines.CoroutineScope,
    listState: androidx.compose.foundation.lazy.LazyListState,
    filterChipFocus: FocusRequester,
    cardReg: FocusRegistry,
    onPlay: (MatchItem) -> Unit,
    onPrev: () -> Unit,
    onNext: () -> Unit,
) {
    val posterIdx = Graph.live.launchPosterIndex
    val heroCtx = androidx.compose.ui.platform.LocalContext.current
    Box(Modifier.fillMaxWidth().height(LIVE_BAND.sx(s))) {
        AsyncImage(
            // v1.10：全局放开硬件位图后，仅 hero 大图按请求走软件解码
            // （超宽海报在个别 GPU 硬件位图解码失败 = 图空白，v1.4 实测）
            model = remember(heroCtx, posterIdx) {
                coil.request.ImageRequest.Builder(heroCtx)
                    .data(Graph.live.posterUrl(posterIdx))
                    .allowHardware(false)
                    .build()
            },
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
            // 需求⑦（2026-09-07）：左右切换比赛时比分/状态内容淡入+轻位移
            // （足球 hero 海报为启动固定池图不随场次换，动画落在内容列上）
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
                        .padding(start = 80f.sx(s), top = 170f.sx(s))
                        .width(760f.sx(s)),
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
                                listOf(animatedHero.league, if (animatedHero.minute.isNotBlank()) animatedHero.minute + "'" else animatedHero.time)
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
            // cta：btn-play 纯白底 268×94 r12 + 实心播放三角
            Box(
                Modifier
                    .align(Alignment.TopStart)
                    .padding(start = 80f.sx(s), top = 500f.sx(s)),
            ) {
                Box(
                    Modifier
                        .focusRequester(heroBtnFocus)
                        .dpadFocusable(
                            scaleFocused = 1.04f, focusedBg = OtvColors.White,
                            // v1.13 方向语义声明：L/R=切换比赛（hero 海报固定不换，仅比分换）；
                            // DOWN=直达当前选中联赛筛选 chip（几何会被比赛卡抢走，需求 足球#2）；
                            // UP=引擎 topFocus 逃逸回当前页 tab（无需声明）
                            navOverride = { dir ->
                                when (dir) {
                                    NAV_LEFT -> if (heroes.size > 1) { onPrev(); true } else false
                                    NAV_RIGHT -> if (heroes.size > 1) { onNext(); true } else false
                                    NAV_DOWN -> {
                                        backScope.launch {
                                            val ok = runCatching { filterChipFocus.requestFocus() }.isSuccess
                                            if (!ok) {
                                                listState.scrollToItem(1)
                                                repeat(10) {
                                                    if (runCatching { filterChipFocus.requestFocus() }.isSuccess) return@launch
                                                    delay(50)
                                                }
                                            }
                                        }
                                        true
                                    }
                                    else -> false
                                }
                            },
                            // 需求 足球#7：光标移到立即观看按钮时页面滚动回最上方
                            onFocusedChange = {
                                if (it) {
                                    ReturnFocus.mark("live", "hero")
                                    backScope.launch { listState.animateScrollToItem(0) }
                                }
                            },
                        ) { onPlay(hero) }
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
        // hero-dots（带内底部，胶囊底）
        if (heroes.size > 1) {
            Row(
                horizontalArrangement = Arrangement.spacedBy(10f.sx(s)),
                modifier = Modifier
                    .align(Alignment.BottomEnd)
                    .padding(end = 40f.sx(s), bottom = 30f.sx(s))
                    .background(OtvColors.TabBarBg, RoundedCornerShape(26f.sx(s)))
                    .padding(horizontal = 12f.sx(s), vertical = 8f.sx(s)),
            ) {
                heroes.forEachIndexed { i, _ ->
                    Box(
                        Modifier
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
    // v1.16：中超 chip = 中超+足协杯（都是中超球队的比赛，用户需求）；
    // 其余具体联赛用 contains（兼容上游「中超联赛」等变体命名）
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
    val short = MATCH_DATE_SHORT_RE.find(monthDay) ?: return value
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

/** 比赛卡（需求 #2：去掉中部「联赛 日期 时间」文案；
 *  需求 #3：直播 = LIVE 徽章(上下居中)/分钟/比分；未开赛 = VS；完场 = 比分。
 *  需求 #6：中部加入联赛名（意甲/意大利杯等）。
 *  需求 足球#3/#1：选中环内缩白色描边（贴卡内缘；行边缘不裁剪）。
 *  需求 足球#5：upToChip 非空（首个分组顶行卡）时 UP 定向回选中 chip——
 *  优先于引擎几何搜索（否则 UP 会被 hero 按钮截走）。 */
@Composable
private fun GameCard(
    nav: NavController,
    m: MatchItem,
    s: Float,
    onPlay: (MatchItem) -> Unit = {},
    upToChip: (() -> Unit)? = null,
    cardReg: FocusRegistry? = null,
) {
    Column(Modifier.width(320f.sx(s))) {
        Box(
            Modifier
                .dpadFocusable(
                    scaleFocused = 1.05f,
                    onFocusedChange = { if (it) ReturnFocus.mark("live", m.matchId) },
                    externalFocusRequester = cardReg?.fr(m.matchId),
                    navOverride = if (upToChip != null) {
                        { dir -> if (dir == NAV_UP) { upToChip(); true } else false }
                    } else null,
                ) { onPlay(m) }
                .background(OtvColors.CardBlack80, RoundedCornerShape(16f.sx(s)))
                .fillMaxWidth()
                .height(146f.sx(s))
                .padding(horizontal = 16f.sx(s)),
            contentAlignment = Alignment.Center,
        ) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
                TeamBlock(m.home, s)
                Column(
                    modifier = Modifier.weight(1f),
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
    val candidates = (nowCal.get(java.util.Calendar.YEAR) - 1..nowCal.get(java.util.Calendar.YEAR) + 1)
        .mapNotNull { makeMillis(it, month, day) }
    val nearby = candidates.filter { kotlin.math.abs(it - nowMillis) <= 31L * 24 * 60 * 60 * 1000 }
    return nearby.minByOrNull { kotlin.math.abs(it - nowMillis) }
        ?: makeMillis(nowCal.get(java.util.Calendar.YEAR), month, day)
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

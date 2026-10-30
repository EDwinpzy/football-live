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
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.togetherWith   // 需求⑦：hero 切换动画 transitionSpec 用
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.model.MatchItem
import com.qiubo.optimaltv.ui.components.FocusRegistry
import com.qiubo.optimaltv.ui.components.InitialFocusEffect
import com.qiubo.optimaltv.ui.components.NAV_DOWN
import com.qiubo.optimaltv.ui.components.NAV_LEFT
import com.qiubo.optimaltv.ui.components.NAV_RIGHT
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
    // 400 设计像素卡宽给队名和联赛名留出空间；1920 宽下每行 4 张并留出安全边距。
    val liveCols = 4
    LaunchedEffect(refreshEpoch) {
        if (refreshEpoch > 0L) vm.refresh(silent = true)
    }
    // 从播放页返回时回到上次聚焦的比赛卡或 hero 按钮。
    val returnKey = remember { ReturnFocus.take("live") }
    val cardReg = remember { FocusRegistry() }
    var restoredFocus by remember { mutableStateOf(false) }
    var initialFocusDone by remember { mutableStateOf(false) }
    val listState = rememberLazyListState()
    val backScope = androidx.compose.runtime.rememberCoroutineScope()
    var heroFocused by remember { mutableStateOf(false) }
    val retryFocus = remember { FocusRequester() }
    val visibleMatches = remember(ui.matches) { visibleLiveScheduleMatches(ui.matches) }
    val groups = remember(visibleMatches) {
        buildMap<String, List<MatchItem>> {
            val zone = java.util.TimeZone.getTimeZone("Asia/Shanghai")
            val fmt = SimpleDateFormat("MM-dd", Locale.CHINA).apply { timeZone = zone }
            put(fmt.format(Date()), emptyList())
            val cal = java.util.Calendar.getInstance(zone); cal.add(java.util.Calendar.DAY_OF_MONTH, 1)
            put(fmt.format(cal.time), emptyList())
            visibleMatches.forEach { m ->
                val date = m.date.ifBlank { "未知" }
                put(date, (get(date) ?: emptyList()) + m)
            }
        }
    }
    val firstVisibleMatchId = groups.values.firstOrNull { it.isNotEmpty() }?.firstOrNull()?.matchId
    val firstCardFocus = firstVisibleMatchId?.let { cardReg.fr(it) }
    val firstCardRowItemIndex = remember(groups) {
        var index = 1 // hero item
        var found = false
        for (matches in groups.values) {
            index += 2 // date header + empty-row placeholder
            if (matches.isNotEmpty()) {
                found = true
                break
            }
        }
        if (found) index else null
    }

    // hero 状态：仅手动左右键切换；按 matchId 记忆（列表刷新重排时海报不再自动换比赛，需求 #11）
    var heroId by rememberSaveable { mutableStateOf<String?>(null) }
    val heroes = remember(ui.matches) { vm.heroList(ui.matches) }
    val heroIdx = heroes.indexOfFirst { it.matchId == heroId }.takeIf { it >= 0 } ?: 0
    val heroBtnFocus = remember { FocusRequester() }
    // 数据就绪后优先落到“立即观看”；没有 hero 时落到第一张可见比赛卡。
    InitialFocusEffect(
        heroBtnFocus,
        "live-primary",
        enabled = returnKey == null && heroes.isNotEmpty(),
    )

    suspend fun focusFirstVisibleCard(): Boolean {
        val requester = firstCardFocus ?: return false
        repeat(20) { attempt ->
            if (runCatching { requester.requestFocus() }.isSuccess) return true
            if (attempt == 0) firstCardRowItemIndex?.let { listState.scrollToItem(it) }
            delay(50)
        }
        return false
    }

    LaunchedEffect(returnKey, heroes.isNotEmpty(), firstCardFocus, firstCardRowItemIndex) {
        if (initialFocusDone || returnKey != null || heroes.isNotEmpty() ||
            firstCardFocus == null || firstCardRowItemIndex == null
        ) return@LaunchedEffect
        initialFocusDone = focusFirstVisibleCard()
    }

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

    // 单页首页只在 hero 上双击退出，首按轻提示。
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
        nav.navigate("player/" + URLEncoder.encode(m.matchId, "UTF-8"))
    }

    InitialFocusEffect(
        retryFocus,
        "live-retry",
        enabled = returnKey == null && !ui.loading && ui.error != null && ui.matches.isEmpty(),
    )
    // 顶栏删除后，OtvNav 的顶层自愈锚点直接指向首个真实内容控件。
    androidx.compose.runtime.DisposableEffect(heroes.isNotEmpty(), ui.loading, ui.error) {
        com.qiubo.optimaltv.ui.components.OtvNav.topFocus = when {
            heroes.isNotEmpty() -> heroBtnFocus
            firstCardFocus != null -> firstCardFocus
            !ui.loading && ui.error != null -> retryFocus
            else -> null
        }
        onDispose { }
    }

    BackHandler {
        val atTop = listState.firstVisibleItemIndex == 0 && listState.firstVisibleItemScrollOffset == 0
        when {
            // 卡片等深层内容 BACK → 返回 hero；hero BACK → 双击退出。
            heroes.isNotEmpty() && (!atTop || !heroFocused) -> backScope.launch {
                listState.scrollToItem(0)
                repeat(10) {
                    if (runCatching { heroBtnFocus.requestFocus() }.isSuccess) return@launch
                    delay(50)
                }
            }
            heroes.isEmpty() && !atTop -> backScope.launch {
                listState.scrollToItem(0)
                focusFirstVisibleCard()
            }
            else -> exitApp()
        }
    }

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {
        when {
            // 规范加载态：居中 brand 色 3 点呼吸 + caption
            ui.loading && ui.matches.isEmpty() -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                com.qiubo.optimaltv.ui.components.BreathingDots(caption = "正在获取赛程")
            }
            ui.error != null && ui.matches.isEmpty() -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text("直播数据加载失败：${ui.error}", color = OtvColors.TextSecondary, fontSize = 20f.sxs(s))
                    Spacer(Modifier.height(24f.sx(s)))
                    Box(
                        Modifier
                            .focusRequester(retryFocus)
                            .dpadFocusable(
                                scaleFocused = 1.04f,
                                onClick = { vm.refresh() },
                            )
                            .background(OtvColors.Brand, RoundedCornerShape(12f.sx(s)))
                            .padding(horizontal = 36f.sx(s), vertical = 18f.sx(s)),
                    ) {
                        Text("重新加载", color = OtvColors.Bg, fontSize = 20f.sxs(s), fontWeight = FontWeight.Medium)
                    }
                }
            }
            else -> {
                // 数据就绪后找回上次聚焦的比赛卡；找不回则落到首屏主操作。
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
                    if (heroes.isNotEmpty()) runCatching { heroBtnFocus.requestFocus() }
                    else focusFirstVisibleCard()
                }
                // 整页一个 LazyColumn：hero 海报带 → 按日期分组比赛
                // v1.13：方向导航统一 OtvNav 引擎，容器仅声明滚动挂靠
                val liveScroll = remember(listState) { lazyNavContainer(listState) }
                // 比赛分组（今日/明日固定分区 + 按日期分组，原版 renderGameRow）
                // 需求 足球#8：今日 = 双行横滑（LazyRow 每列上下两卡）；明日及以后每行 4 卡，
                // 行数不限，避免固定 5 列把卡片挤窄。
                // v1.19 流畅度：分组结果在 LazyColumn 外 remember，避免状态变化时反复建组。
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
                            backScope = backScope,
                            onHeroFocused = { heroFocused = it },
                            listState = listState,
                            onFocusFirstMatch = { backScope.launch { focusFirstVisibleCard() } },
                            onPlay = ::playMatch,
                            onPrev = {
                                if (heroes.isNotEmpty()) heroId = heroes[(heroIdx - 1 + heroes.size) % heroes.size].matchId
                            },
                            onNext = {
                                if (heroes.isNotEmpty()) heroId = heroes[(heroIdx + 1) % heroes.size].matchId
                            },
                        )
                    }
                    // 比赛分组结果见 LazyColumn 外的 groups（v1.19 提级 remember）
                    var groupIdx = 0
                    groups.forEach { (date, ms) ->
                        val isFirstGroup = groupIdx++ == 0
                        item(key = "date-$date") {
                            // 规范「日期分组头」：brand 6dp 圆点 + label 字号，上下留白充足
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                modifier = Modifier.padding(start = 90f.sx(s), top = 40f.sx(s), bottom = 20f.sx(s)),
                            ) {
                                Box(
                                    Modifier
                                        .size(6f.sx(s))
                                        .background(OtvColors.Brand, CircleShape),
                                )
                                Spacer(Modifier.width(12f.sx(s)))
                                Text(dateLabel(date), color = OtvColors.TextSecondary, fontSize = 15f.sxs(s), fontWeight = FontWeight.Medium)
                            }
                        }
                        item(key = "row-$date-h") {
                            if (ms.isEmpty()) {
                                // 规范空分组：caption 居中
                                Text(
                                    "暂无比赛", color = OtvColors.TextDisabled, fontSize = 13f.sxs(s),
                                    textAlign = TextAlign.Center,
                                    modifier = Modifier.fillMaxWidth().padding(top = 20f.sx(s), bottom = 20f.sx(s)),
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
                                                pages[pi].rows.forEach { row ->
                                                    Row(horizontalArrangement = Arrangement.spacedBy(40f.sx(s))) {
                                                        row.forEach { match ->
                                                            GameCard(match, s, ::playMatch, cardReg = cardReg)
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        } else if (ms.isNotEmpty()) {
                            // 明日及以后：按 400 设计像素卡宽计算列数；1920 宽下为 4 列。
                            // v1.10（2026-08-31）：逐行独立 item（原为单个巨 item 内非 lazy Column，
                            // 比赛多时整页一次性组合全部行，低端机切页/滚动卡顿）；间距 1:1 保留
                            // （首行 top24 / 行距 28 / 组间由下一组日期标题 top40 衔接）
                            ms.chunked(liveCols).forEachIndexed { ri, rowMs ->
                                item(key = "row-$date-$ri") {
                                    Row(
                                        horizontalArrangement = Arrangement.spacedBy(36f.sx(s)),
                                        modifier = Modifier
                                            .padding(top = if (ri == 0) 24f.sx(s) else 28f.sx(s))
                                            .padding(horizontal = 86f.sx(s)),
                                    ) {
                                        rowMs.forEach { m ->
                                            GameCard(m, s, ::playMatch, cardReg = cardReg)
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
        OtvHint(hint)
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
    backScope: kotlinx.coroutines.CoroutineScope,
    listState: androidx.compose.foundation.lazy.LazyListState,
    onFocusFirstMatch: () -> Unit,
    onHeroFocused: (Boolean) -> Unit,
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
                                color = OtvColors.TextPrimary, fontSize = 20f.sxs(s), fontWeight = FontWeight.SemiBold,
                            )
                        } else {
                            Text(
                                animatedHero.displayStatus,
                                color = OtvColors.TextSecondary, fontSize = 20f.sxs(s), fontWeight = FontWeight.SemiBold,
                            )
                            Spacer(Modifier.width(14f.sx(s)))
                            Text(
                                matchTimeLabel(animatedHero),
                                color = OtvColors.TextSecondary, fontSize = 20f.sxs(s), fontWeight = FontWeight.SemiBold,
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
                            scaleFocused = 1.04f,
                            // focusedBg 留空 → 聚焦画 brand 描边环（规范：brand=强调）
                            // v1.13 方向语义声明：L/R=切换比赛（hero 海报固定不换，仅比分换）；
                            // DOWN=直达第一张可见比赛卡；
                            // UP=顶部无更多控件时由引擎原地封锁，不离开首屏主操作。
                            navOverride = { dir ->
                                when (dir) {
                                    NAV_LEFT -> if (heroes.size > 1) { onPrev(); true } else false
                                    NAV_RIGHT -> if (heroes.size > 1) { onNext(); true } else false
                                    NAV_DOWN -> {
                                        onFocusFirstMatch()
                                        true
                                    }
                                    else -> false
                                }
                            },
                            // 需求 足球#7：光标移到立即观看按钮时页面滚动回最上方
                            onFocusedChange = {
                                onHeroFocused(it)
                                if (it) {
                                    ReturnFocus.mark("live", "hero")
                                    backScope.launch { listState.animateScrollToItem(0) }
                                }
                            },
                        ) { onPlay(hero) }
                        .background(OtvColors.Brand, RoundedCornerShape(12f.sx(s)))
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
                        Text("立即观看", color = OtvColors.Bg, fontSize = 20f.sxs(s), fontWeight = FontWeight.Medium)
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
                    .background(OtvColors.Overlay, RoundedCornerShape(26f.sx(s)))
                    .padding(horizontal = 12f.sx(s), vertical = 8f.sx(s)),
            ) {
                heroes.forEachIndexed { i, _ ->
                    Box(
                        Modifier
                            .size(10f.sx(s))
                            .background(if (i == heroIdx) OtvColors.TextPrimary else OtvColors.TextDisabled, CircleShape),
                    )
                }
            }
        }
    }
}

/** LIVE 徽标（规范：live 底白字胶囊 + 左侧 6dp 呼吸圆点；高 28、全弧圆角、label 字号） */
@Composable
private fun LiveBadge(s: Float) {
    val breath = rememberInfiniteTransition(label = "liveDot").animateFloat(
        initialValue = 0.3f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(800),
            repeatMode = RepeatMode.Reverse,
        ),
        label = "liveDotAlpha",
    )
    Box(
        Modifier
            .background(OtvColors.Live, RoundedCornerShape(50f.sx(s)))
            .padding(horizontal = 12f.sx(s), vertical = 5f.sx(s)),
        contentAlignment = Alignment.Center,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                Modifier
                    .size(6f.sx(s))
                    .background(OtvColors.Bg.copy(alpha = breath.value), CircleShape),
            )
            Spacer(Modifier.width(6f.sx(s)))
            Text(
                "LIVE", color = OtvColors.Bg, fontSize = 15f.sxs(s), fontWeight = FontWeight.Bold,
                lineHeight = 18f.sxs(s),
            )
        }
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
                Text(team.take(1), color = OtvColors.TextPrimary, fontSize = 40f.sxs(s), fontWeight = FontWeight.Bold)
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
        Text(team, color = OtvColors.TextPrimary, fontSize = nameSize.sxs(s), fontWeight = FontWeight.Bold, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        if (pts.isNotBlank()) {
            Spacer(Modifier.width(12f.sx(s)))
            Text(pts, color = OtvColors.TextPrimary, fontSize = 76f.sxs(s), fontWeight = FontWeight.Bold)
        }
    }
}

/** 直播列表仅显示未开赛或正在进行的赛事。 */
fun visibleLiveScheduleMatches(matches: List<MatchItem>): List<MatchItem> =
    matches.filter { !it.isFinished }

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
        today -> "今日 · ${matchDateDisplay(date)}"
        tomorrow -> "明日 · ${matchDateDisplay(date)}"
        else -> matchDateDisplay(date).takeIf(String::isNotBlank) ?: "比赛"
    }
}

/** 比赛卡（400×146，给两侧队名和中部联赛信息留出宽度；
 *  需求 #2：去掉中部「联赛 日期 时间」文案；
 *  需求 #3：直播 = LIVE 徽章(上下居中)/分钟/比分；未开赛 = VS；完场 = 比分。
 *  需求 #6：中部加入联赛名（意甲/意大利杯等）。
 *  需求 足球#3/#1：选中环内缩白色描边（贴卡内缘；行边缘不裁剪）。
 */
@Composable
private fun GameCard(
    m: MatchItem,
    s: Float,
    onPlay: (MatchItem) -> Unit = {},
    cardReg: FocusRegistry? = null,
) {
    // 规范焦点态：surfaceFocus 背景 + brand 描边（dpadFocusable 环）+ scale 1.02，无阴影发光堆叠
    var cardFocused by remember { mutableStateOf(false) }
    Column(Modifier.width(400f.sx(s))) {
        Box(
            Modifier
                .dpadFocusable(
                    scaleFocused = 1.02f,
                    onFocusedChange = {
                        cardFocused = it
                        if (it) ReturnFocus.mark("live", m.matchId)
                    },
                    externalFocusRequester = cardReg?.fr(m.matchId),
                ) { onPlay(m) }
                .background(
                    if (cardFocused) OtvColors.SurfaceFocus else OtvColors.Surface,
                    RoundedCornerShape(18f.sx(s)),
                )
                .fillMaxWidth()
                .height(146f.sx(s))
                .padding(horizontal = 16f.sx(s)),
            contentAlignment = Alignment.Center,
        ) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
                TeamBlock(m.home, s, width = 112f)
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
                            m.league, color = OtvColors.TextSecondary, fontSize = 20f.sxs(s), lineHeight = 22f.sxs(s),
                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                        )
                    }
                    if (m.isLive) {
                        LiveBadge(s)
                        if (m.minute.isNotBlank()) {
                            Text(m.minute + "'", color = OtvColors.TextPrimary, fontSize = 26f.sxs(s), lineHeight = 28f.sxs(s), fontWeight = FontWeight.SemiBold)
                        }
                        if (m.hasScore) {
                            Text(
                                "${m.homeScore} - ${m.awayScore}",
                                color = OtvColors.TextPrimary, fontSize = 34f.sxs(s), lineHeight = 36f.sxs(s), fontWeight = FontWeight.Bold,
                            )
                        }
                    } else if (m.isFinished) {
                        // 完赛：比分灰（规范——比分不做胜负语义色）
                        if (m.hasScore) {
                            Text(
                                "${m.homeScore} - ${m.awayScore}",
                                color = OtvColors.TextSecondary, fontSize = 34f.sxs(s), lineHeight = 36f.sxs(s), fontWeight = FontWeight.Bold,
                            )
                        } else {
                            Text("完场", color = OtvColors.TextDisabled, fontSize = 30f.sxs(s), lineHeight = 32f.sxs(s), fontWeight = FontWeight.Bold)
                        }
                    } else if (m.isClockHeld) {
                        Text(m.displayStatus, color = OtvColors.TextDisabled, fontSize = 30f.sxs(s), lineHeight = 32f.sxs(s), fontWeight = FontWeight.Bold)
                    } else if (m.hasScore) {
                        Text(
                            "${m.homeScore} - ${m.awayScore}",
                            color = OtvColors.TextPrimary, fontSize = 34f.sxs(s), lineHeight = 36f.sxs(s), fontWeight = FontWeight.Bold,
                        )
                    } else {
                        Text("VS", color = OtvColors.TextDisabled, fontSize = 30f.sxs(s), lineHeight = 32f.sxs(s), fontWeight = FontWeight.Bold)
                    }
                    // 2026-09-05：未开赛/完场卡片显示开赛时间（直播中已有 LIVE 徽章/分钟数）
                    if (!m.isLive && m.time.isNotBlank()) {
                        Text(m.time, color = OtvColors.TextDisabled, fontSize = 20f.sxs(s), lineHeight = 22f.sxs(s), fontWeight = FontWeight.Medium)
                    }
                }
                TeamBlock(m.away, s, width = 112f)
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
private fun TeamBlock(team: String, s: Float, width: Float = 112f) {
    Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.width(width.sx(s))) {
        Box(
            Modifier.size(72f.sx(s)),
            contentAlignment = Alignment.Center,
        ) {
            // 2026-09-18 队标重叠修复：同 TeamIcon 行，兜底首字只在未加载出队徽时显示
            var iconReady by remember(team) { mutableStateOf(false) }
            if (!iconReady) {
                Text(team.take(1), color = OtvColors.TextPrimary, fontSize = 34f.sxs(s), fontWeight = FontWeight.Bold)
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
            team, color = OtvColors.TextPrimary, textAlign = TextAlign.Center,
            maxLines = if (wrap) 2 else 1,
            overflow = if (wrap) TextOverflow.Ellipsis else TextOverflow.Clip,
            lineHeight = (if (wrap) 17f else fitSize).sxs(s),
            fontSize = (if (wrap) 14f else fitSize).sxs(s),
        )
    }
}

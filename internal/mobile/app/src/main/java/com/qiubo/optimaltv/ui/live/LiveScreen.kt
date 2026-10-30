package com.qiubo.optimaltv.ui.live

import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
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
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
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
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.model.MatchItem
import com.qiubo.optimaltv.ui.components.BrandDotsLoading
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.tapCardSurface
import com.qiubo.optimaltv.ui.theme.Bg
import com.qiubo.optimaltv.ui.theme.Brand
import com.qiubo.optimaltv.ui.theme.LiveRed
import com.qiubo.optimaltv.ui.theme.SurfaceDark
import com.qiubo.optimaltv.ui.theme.SurfaceFocus
import com.qiubo.optimaltv.ui.theme.TextDisabled
import com.qiubo.optimaltv.ui.theme.TextPrimary
import com.qiubo.optimaltv.ui.theme.TextSecondary
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

    /** 页面每秒检查一次，但每次失败后至少间隔 9.5 秒才重试。 */
    fun refreshIfStale() {
        if (liveRefreshDue(lastRefreshAttemptAt, System.currentTimeMillis())) refresh(silent = true)
    }
    fun projectClock(now: Long = System.currentTimeMillis()) {
        val old = _ui.value
        val next = old.matches.map { MatchClockPolicy.project(it, matchStartMillis(it), now) }
        if (next != old.matches) _ui.value = old.copy(matches = next)
    }
}

/* ---------------- Screen（SPEC v1 极简清爽：顶栏 + 日期分组 + 单行比赛卡） ---------------- */

@Composable
fun LiveScreen(nav: NavController, vm: LiveViewModel = viewModel()) {
    val ui by vm.ui.collectAsStateWithLifecycle()
    val refreshEpoch by com.qiubo.optimaltv.lifecycle.AppLifecycleManager.refreshEpoch.collectAsStateWithLifecycle()
    LaunchedEffect(refreshEpoch) {
        if (refreshEpoch > 0L) vm.refresh(silent = true)
    }

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

    // 页面前台每秒检查刷新条件；成功或失败后约 10 秒再拉取，离开本页自动停止。
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
        nav.navigate("player/" + URLEncoder.encode(m.matchId, "UTF-8"))
    }

    // 分组提级 remember，避免列表重组时重复计算。
    val groups = remember(ui.matches) {
        val filtered = visibleLiveScheduleMatches(ui.matches)
        buildMap<String, List<MatchItem>> {
            val zone = java.util.TimeZone.getTimeZone("Asia/Shanghai")
            val fmt = SimpleDateFormat("MM-dd", Locale.CHINA).apply { timeZone = zone }
            put(fmt.format(Date()), emptyList<MatchItem>())
            val cal = java.util.Calendar.getInstance(zone); cal.add(java.util.Calendar.DAY_OF_MONTH, 1)
            put(fmt.format(cal.time), emptyList<MatchItem>())
            filtered.forEach { m -> put(m.date.ifBlank { "未知" }, (get(m.date.ifBlank { "未知" }) ?: emptyList()) + m) }
        }
    }

    Box(Modifier.fillMaxSize().background(Bg)) {
        when {
            ui.loading && ui.matches.isEmpty() -> {
                Column(
                    Modifier.fillMaxSize(),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.Center,
                ) {
                    BrandDotsLoading()
                    Spacer(Modifier.height(14.dp))
                    Text("正在获取赛程", color = TextSecondary, fontSize = 11.sp)
                }
            }
            ui.error != null && ui.matches.isEmpty() -> {
                Column(
                    Modifier.fillMaxSize(),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.Center,
                ) {
                    Text("赛程加载失败", color = TextPrimary, fontSize = 17.sp, fontWeight = FontWeight.SemiBold)
                    Spacer(Modifier.height(6.dp))
                    Text(
                        ui.error ?: "",
                        color = TextSecondary, fontSize = 11.sp,
                        textAlign = TextAlign.Center,
                        modifier = Modifier.padding(horizontal = 32.dp),
                    )
                }
            }
            else -> {
                // 整页一个 LazyColumn：按日期分组的单行比赛卡
                LazyColumn(
                    state = listState,
                    modifier = Modifier
                        .fillMaxSize()
                        .background(Bg),
                    contentPadding = PaddingValues(
                        top = 16.dp,
                        bottom = 32.dp,
                        start = 16.dp,
                        end = 16.dp,
                    ),
                ) {
                    // 比赛分组结果见上方提级 remember 的 groups（v1.19 流畅度）
                    groups.forEach { (date, ms) ->
                        item(key = "date-$date") {
                            GroupHeader(date)
                        }
                        if (ms.isEmpty()) {
                            item(key = "empty-$date") {
                                Text(
                                    "暂无比赛",
                                    color = TextDisabled, fontSize = 11.sp,
                                    textAlign = TextAlign.Center,
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .padding(top = 20.dp, bottom = 20.dp),
                                )
                            }
                        } else {
                            items(ms, key = { "m-${it.matchId}" }) { m ->
                                MatchRow(m, onPlay = ::playMatch)
                            }
                        }
                    }
                }
            }
        }
        OtvHint(hint)
    }
}

/** 日期分组头（SPEC v1：brand 色 6dp 圆点 + label 字号，上下留白 20dp） */
@Composable
private fun GroupHeader(date: String) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.padding(top = 20.dp, bottom = 12.dp),
    ) {
        Box(
            Modifier
                .size(6.dp)
                .clip(CircleShape)
                .background(Brand),
        )
        Spacer(Modifier.width(8.dp))
        Text(groupTitle(date), color = TextSecondary, fontSize = 13.sp, fontWeight = FontWeight.Medium)
    }
}

/** 分组标题 = 「今日 · 10月23日」/「明日 · …」/「10月25日」 */
private fun groupTitle(date: String): String {
    val zone = java.util.TimeZone.getTimeZone("Asia/Shanghai")
    val fmt = SimpleDateFormat("MM-dd", Locale.CHINA).apply { timeZone = zone }
    val today = fmt.format(Date())
    val cal = java.util.Calendar.getInstance(zone); cal.add(java.util.Calendar.DAY_OF_MONTH, 1)
    val tomorrow = fmt.format(cal.time)
    val monthDay = matchDateDisplay(date).ifBlank { date.trim() }
    return when (matchDateMonthDay(date)) {
        today -> "今日 · $monthDay"
        tomorrow -> "明日 · $monthDay"
        else -> monthDay
    }
}

/**
 * 比赛卡（SPEC v1「比赛卡片」手机规格）：单行三段布局，高 ≥84dp，圆角 18，无阴影；
 * 左：联赛·时间（label/次级）；中：主队 比分或开赛时间 客队（title）；右：状态徽标。
 * 按压态 = surfaceFocus 背景、无 scale；卡片间距 12dp。
 */
@Composable
private fun MatchRow(
    m: MatchItem,
    onPlay: (MatchItem) -> Unit,
) {
    val shape = RoundedCornerShape(18.dp)
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .padding(bottom = 12.dp)
            .heightIn(min = 84.dp)
            .tapCardSurface(SurfaceDark, SurfaceFocus, shape) { onPlay(m) }
            .padding(horizontal = 20.dp, vertical = 12.dp),
    ) {
        // 左：联赛 · 时间
        Column(Modifier.width(64.dp)) {
            Text(
                m.league.ifBlank { "足球" },
                color = TextSecondary, fontSize = 13.sp, fontWeight = FontWeight.Medium,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(2.dp))
            Text(
                m.time,
                color = TextDisabled, fontSize = 11.sp,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
            )
        }
        Spacer(Modifier.width(16.dp))
        // 中：主队 比分/开赛时间 客队（比分不做胜负语义色，一律 textPrimary；完赛比分灰）
        Row(Modifier.weight(1f), verticalAlignment = Alignment.CenterVertically) {
            Text(
                m.home,
                color = TextPrimary, fontSize = 22.sp, fontWeight = FontWeight.SemiBold,
                textAlign = TextAlign.End,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.width(10.dp))
            MiddleScore(m)
            Spacer(Modifier.width(10.dp))
            Text(
                m.away,
                color = TextPrimary, fontSize = 22.sp, fontWeight = FontWeight.SemiBold,
                textAlign = TextAlign.Start,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
        }
        Spacer(Modifier.width(12.dp))
        StatusBadge(m)
    }
}

/** 中段：直播中/完赛=比分（完赛灰），其余=开赛时间；无比分直播中显示分钟数 */
@Composable
private fun MiddleScore(m: MatchItem) {
    when {
        m.isLive && m.hasScore -> MiddleText("${m.homeScore} - ${m.awayScore}", TextPrimary)
        m.isLive && m.minute.isNotBlank() -> MiddleText(m.minute + "'", TextSecondary, 13)
        m.isFinished && m.hasScore -> MiddleText("${m.homeScore} - ${m.awayScore}", TextSecondary)
        else -> MiddleText(m.time, TextPrimary)
    }
}

@Composable
private fun MiddleText(text: String, color: Color, size: Int = 17) {
    Text(
        text,
        color = color, fontSize = size.sp,
        fontWeight = FontWeight.SemiBold,
        textAlign = TextAlign.Center,
        maxLines = 1,
    )
}

/** 状态徽标（SPEC v1「状态徽标」）：高 28dp 全弧胶囊、内 padding 12dp、label 字号；
 *  LIVE = 红色底白字 + 左侧 6dp 白色呼吸圆点；未开赛/完赛 = textDisabled 弱化文字 */
@Composable
private fun StatusBadge(m: MatchItem) {
    when {
        m.isLive -> {
            val dot = rememberInfiniteTransition(label = "liveDot")
            val dotAlpha by dot.animateFloat(
                initialValue = 0.25f,
                targetValue = 1f,
                animationSpec = infiniteRepeatable(
                    animation = tween(900),
                    repeatMode = RepeatMode.Reverse,
                ),
                label = "liveDotAlpha",
            )
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .background(LiveRed, RoundedCornerShape(50))
                    .padding(horizontal = 12.dp, vertical = 5.dp),
            ) {
                Box(
                    Modifier
                        .size(6.dp)
                        .clip(CircleShape)
                        .background(Color.White.copy(alpha = dotAlpha)),
                )
                Spacer(Modifier.width(6.dp))
                Text("LIVE", color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.Bold)
            }
        }
        m.isFinished -> BadgeText("完赛")
        m.isClockHeld -> BadgeText(m.displayStatus)
        else -> BadgeText("未开赛")
    }
}

@Composable
private fun BadgeText(text: String) {
    Text(text, color = TextDisabled, fontSize = 13.sp, fontWeight = FontWeight.Medium, maxLines = 1)
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
    val short = MATCH_DATE_SHORT_RE.find(monthDay) ?: return ""
    return "${short.groupValues[1].toInt()}月${short.groupValues[2].toInt()}日"
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

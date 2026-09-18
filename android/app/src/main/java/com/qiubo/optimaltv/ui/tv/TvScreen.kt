package com.qiubo.optimaltv.ui.tv

import android.view.KeyEvent
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.border
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
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
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
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.OtvLog
import com.qiubo.optimaltv.data.repo.IptvRepository
import com.qiubo.optimaltv.data.repo.LineSpeed
import com.qiubo.optimaltv.playback.EnginePlayState
import com.qiubo.optimaltv.ui.components.AppleLoading
import com.qiubo.optimaltv.ui.components.FocusRegistry
import com.qiubo.optimaltv.ui.components.InitialFocusEffect
import com.qiubo.optimaltv.ui.components.NAV_DOWN
import com.qiubo.optimaltv.ui.components.NAV_LEFT
import com.qiubo.optimaltv.ui.components.NAV_RIGHT
import com.qiubo.optimaltv.ui.components.NAV_UP
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.OtvNav
import com.qiubo.optimaltv.ui.components.ProvideNavScrolls
import com.qiubo.optimaltv.ui.components.dpadFocusable
import com.qiubo.optimaltv.ui.components.lazyNavContainer
import com.qiubo.optimaltv.ui.components.lazyGridNavContainer
import com.qiubo.optimaltv.ui.components.navSink
import com.qiubo.optimaltv.ui.components.navigateToTab
import com.qiubo.optimaltv.ui.detail.simpleFactory
import com.qiubo.optimaltv.ui.player.PlayerViewModel
import com.qiubo.optimaltv.ui.theme.AccentBlue
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * 电视页（v1.20 2026-09-03 交互重做；2026-09-04 需求④⑥ 调整，与移动版同构）：
 * 双态结构——
 * ① 浏览态（进入默认）：不直接进直播——整页两级选台器（需求④ 标题区已去除；需求⑥
 *    一级分组纵向列 + 二级频道纵向列表两列布局），OK 点击频道才进入直播画面；
 *    顶栏 tab 恢复（与其他主页面一致，可正常切页）。
 * ② 播放态（选中频道后）：全屏沉浸直播（v1.16 行为）——隐藏 tab 栏，UP/DOWN 直接
 *    换台，LEFT/OK/MENU 呼出左侧两级侧边栏选台；BACK 双击返回（提示「再按一次返回」，
 *    电视#2）。
 *
 * 浏览态焦点（OtvNav）：分组列 hover-select 切分组（与影视 chips 同款）；分组 RIGHT
 * 定向进频道列首行；频道 LEFT 定向回选中分组 chip；tab DOWN 定向进内容首焦点。
 *
 * 播放态健壮性与播放链路保持 v1.19 全量：bootIptv + UrlGuard 双层校验 + 直播看门狗/
 * 卡顿检测/30s 自动重试/软解→libVLC 三段回退、起播失败 12s 自动跳台（3 次上限）、
 * 电视#5 实时测速（换台按分数选线 + 3.5s 缓冲无感热切换 + 驻留轮询）。
 */
@Composable
fun TvScreen(nav: NavController) {
    val iptv = Graph.iptv
    val ui by iptv.state.collectAsStateWithLifecycle()
    val s = rememberUiScale()
    val scope = rememberCoroutineScope()

    /* ---------- 播放会话：页面内嵌 PlayerViewModel（数据就绪后显式起播） ---------- */
    val vm: PlayerViewModel = viewModel(
        key = "tv-iptv",
        factory = simpleFactory {
            PlayerViewModel("iptv:", 0, Graph.repo, Graph.db, Graph.settings, autoBoot = false)
        },
    )
    val vmUi by vm.ui.collectAsStateWithLifecycle()

    // v1.17 二重门控（公测版）：电视页为内嵌播放不走 player 路由——授权中途失效
    // （联网复验命中吊销降级/过期）立即停播；内测版编译期剔除
    if (com.qiubo.optimaltv.BuildConfig.LICENSE_ENABLED) {
        LaunchedEffect(Unit) {
            com.qiubo.optimaltv.license.LicenseManager.state.collect {
                if (it != null && !com.qiubo.optimaltv.license.LicenseManager.isPremium()) vm.onHostPause()
            }
        }
    }

    // KEEP_SCREEN_ON：直播场景防息屏（对齐播放页）
    val activity = androidx.compose.ui.platform.LocalContext.current as android.app.Activity
    DisposableEffect(Unit) {
        activity.window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        onDispose { activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON) }
    }

    // 后台暂停/回来自动恢复（对齐播放页）
    val lifecycleOwner = androidx.compose.ui.platform.LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val obs = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_PAUSE -> vm.onHostPause()
                Lifecycle.Event.ON_RESUME -> vm.onHostResume()
                else -> Unit
            }
        }
        lifecycleOwner.lifecycle.addObserver(obs)
        onDispose { lifecycleOwner.lifecycle.removeObserver(obs) }
    }

    /* ---------- 当前播放频道（侧边栏高亮/UP-DOWN 换台基准） ---------- */
    var playingGroup by remember { mutableStateOf("") }
    var playingName by remember { mutableStateOf("") }
    var playingUrl by remember { mutableStateOf("") }
    val playingKey = playingName + "|" + playingUrl
    /** 电视#5：当前线路测速分数与自动切换守卫（每频道一次） */
    var playingLineScore by remember { mutableStateOf<Long?>(null) }
    var autoSwapped by remember { mutableStateOf(false) }

    /* ---------- 双态：浏览（进入默认）/ 播放 ---------- */
    var playing by remember { mutableStateOf(false) }

    /* ---------- 侧边栏状态（播放态） ---------- */
    var sidebarVisible by remember { mutableStateOf(false) }
    var groupSel by rememberSaveable(ui.groups) { mutableIntStateOf(0) }
    // 电视#7：一二级菜单间切换后默认选中上次选中的——首次呼出定位到正在播放的
    // 分组/频道，用户浏览过分组后就记住浏览位置，不再拽回播放分组
    var hasBrowsed by rememberSaveable { mutableStateOf(false) }
    var lastChKey by rememberSaveable { mutableStateOf("") }   // 上次聚焦的频道行 "分组|名"
    val selGroup = ui.groups.getOrNull(groupSel) ?: ""
    val groupChannels = remember(ui.channels, selGroup) { ui.channels.filter { it.group == selGroup } }
    var pendingChIdx by remember { mutableIntStateOf(-1) }    // 呼出时定向落焦的频道行（-1=首行）
    var focusTick by remember { mutableIntStateOf(0) }        // 定向落焦触发器（呼出/分组 RIGHT）

    var hint by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(hint) { if (hint != null) { delay(2500); hint = null } }
    var lastBackAt by remember { mutableLongStateOf(0L) }   // 双击 BACK 判定（2s 窗口）

    val groupListState = rememberLazyListState()
    val chListState = rememberLazyListState()
    val rootSink = remember { FocusRequester() }
    val cardReg = remember { FocusRegistry() }   // key: 频道 "分组|名" / 分组 "g:索引"
    val firstChannelKey = groupChannels.firstOrNull()?.let { it.group + "|" + it.name }

    /* ---------- 需求⑩（2026-09-07）：播放态控件浮层（投屏/选台两钮）整体移除——
        OK/MENU/触屏点画面直接呼出选台侧边栏，不再有中间浮层；投屏入口随之移除 ---------- */

    // 浏览态：顶栏 tab 为全局「到顶逃逸」目标（MainTabBar 注册）；播放态：清空防甩出页外
    LaunchedEffect(playing) { if (playing) OtvNav.topFocus = null }

    /* ---------- 换台动作（写记忆 + bootIptv 全链路；v1.16 带同频道备用线路） ---------- */
    /** 侧边栏定位到正在播放的频道（分组切到播放分组 + 定向落焦该行） */
    fun locatePlayingInSidebar() {
        val gi = ui.groups.indexOf(playingGroup).takeIf { it >= 0 } ?: 0
        pendingChIdx = ui.channels
            .filter { it.group == (ui.groups.getOrNull(gi) ?: "") }
            .indexOfFirst { it.name == playingName }
        groupSel = gi
        focusTick++
    }

    /** [lineIdx] = 起播线路（0=主线路；恢复上次线路时 >0）；其余线路作为备用线传给 VM。
     *  电视#5 实时测速：主线路失败时按测速分数取最快（iptv.rankUrls，失败线殿后），
     *  起播后异步补测全部线路（下次换台/自动切换即有分数可用）。
     *  v1.20：浏览态选中频道 → playing=true 进入直播画面（电视#1）。 */
    fun startPlay(group: String, ch: IptvRepository.Channel, keepSidebar: Boolean, lineIdx: Int = 0) {
        val ranked = iptv.rankUrls(ch.urls)
        val urls = if (ranked.first() == (ch.urls.getOrNull(lineIdx) ?: ch.urls.first())) ranked else ch.urls
        val urlIdx = urls.indexOf(ch.urls.getOrNull(lineIdx) ?: ch.urls.firstOrNull()).takeIf { it >= 0 } ?: 0
        val url = urls.getOrNull(urlIdx) ?: urls.firstOrNull() ?: return
        playingGroup = group
        playingName = ch.name
        playingUrl = url
        playingLineScore = LineSpeed.scoreOf(url)
        autoSwapped = false
        playing = true
        hint = ch.name   // 换台反馈：底部轻提示频道名
        scope.launch { runCatching { Graph.settings.rememberIptvLast(group, ch.name, url) } }
        scope.launch { iptv.probeAll(ch.urls) }   // 电视#5：后台补测本频道全部线路
        vm.switchIptv(ch.name, url, urls.filterIndexed { i, _ -> i != urlIdx })
        if (keepSidebar) locatePlayingInSidebar() else sidebarVisible = false
    }

    /** 画面态 UP/DOWN 直接换台：全频道表顺序 ±1（环形） */
    fun skipChannel(delta: Int) {
        val all = ui.channels
        if (all.isEmpty()) return
        val idx = all.indexOfFirst { it.name == playingName && it.group == playingGroup }
        val ch = all[if (idx < 0) 0 else (idx + delta).mod(all.size)]
        startPlay(ch.group, ch, keepSidebar = false)
    }

    /** 呼出侧边栏：电视#7——浏览过则停在上次浏览的分组/频道行，首次定位到正在播放 */
    fun openSidebar() {
        if (!hasBrowsed) {
            locatePlayingInSidebar()
        } else {
            // 回到上次浏览分组；频道行优先回上次聚焦行（该分组仍在时），否则首行
            pendingChIdx = if (lastChKey.isBlank()) -1
            else groupChannels.indexOfFirst { it.group + "|" + it.name == lastChKey }
            focusTick++
        }
        sidebarVisible = true
    }

    /* ---------- 电视#5：起播慢时自动切最快线路 ----------
     * 起播 3.5s 仍 BUFFERING，且已测出明显更快的线路（分数 < 当前线 55%）→ 无感
     * 热切换（引擎复用不重建渲染面）。每频道只自动切一次，防止抖动循环。 */
    LaunchedEffect(playingKey, vmUi.playState) {
        if (autoSwapped || vmUi.playState != EnginePlayState.BUFFERING || vmUi.fatalMsg != null) return@LaunchedEffect
        kotlinx.coroutines.delay(3_500)
        if (autoSwapped || vm.ui.value.playState != EnginePlayState.BUFFERING ||
            vm.ui.value.fatalMsg != null || vm.ui.value.isPlaying
        ) return@LaunchedEffect
        val ch = ui.channels.firstOrNull { it.name == playingName && it.group == playingGroup } ?: return@LaunchedEffect
        val cur = playingLineScore
        val best = iptv.rankUrls(ch.urls).firstOrNull { it != playingUrl && LineSpeed.scoreOf(it)?.let { sc -> sc < LineSpeed.FAIL_MS } == true }
        val bestScore = best?.let { LineSpeed.scoreOf(it) }
        if (best != null && bestScore != null && bestScore < LineSpeed.FAIL_MS &&
            (cur == null || bestScore < cur * 0.55f)
        ) {
            autoSwapped = true
            OtvLog.i("tv 测速自动换线: ${playingName} ${cur ?: -1}ms → ${bestScore}ms")
            hint = "已自动切换最快线路"
            vm.switchIptv(ch.name, best, ch.urls.filter { it != playingUrl && it != best })
        }
    }

    /* ---------- 电视#5：驻留期轮询测速（当前分组频道，滚动刷新分数） ---------- */
    LaunchedEffect(selGroup, ui.channels) {
        while (true) {
            val targets = ui.channels.filter { it.group == selGroup }.flatMap { it.urls }
            if (targets.isNotEmpty()) iptv.probeAll(targets.take(40))
            kotlinx.coroutines.delay(60_000)
        }
    }

    /* ---------- 侧边栏落焦/收焦：呼出定向到播放行；收起回画面态键宿 ---------- */
    // keys 含 playing（2026-09-04 修复）：浏览态起播（playing=true）后原焦点随浏览态
    // 组合销毁而死亡，effect 旧 keys（sidebarVisible/focusTick）均未变不重跑 → 键宿
    // 拿不到焦点，LEFT/OK/MENU 呼不出侧边栏、UP/DOWN 不换台（v1.20 浏览态引入后遗留）
    LaunchedEffect(sidebarVisible, focusTick, playing) {
        if (!sidebarVisible) {
            if (playing) runCatching { rootSink.requestFocus() }
            return@LaunchedEffect
        }
        // 等频道列表组合（快照未就绪时列表为空，等自动起播流程 focusTick 再触发）
        var waitedMs = 0
        while (chListState.layoutInfo.totalItemsCount == 0 && waitedMs < 3000) {
            delay(100); waitedMs += 100
        }
        if (chListState.layoutInfo.totalItemsCount == 0) return@LaunchedEffect
        if (pendingChIdx >= 0) chListState.scrollToItem(pendingChIdx) else chListState.scrollToItem(0)
        // 电视#7：呼出落焦目标——首次=正在播放行；浏览过=上次聚焦行；否则首行
        val key = when {
            pendingChIdx >= 0 && hasBrowsed -> lastChKey.ifBlank { null }
            pendingChIdx >= 0 -> playingGroup + "|" + playingName
            else -> firstChannelKey
        }
        val fr = key?.let { cardReg.fr(it) }
        if (fr != null) {
            repeat(20) {
                if (runCatching { fr.requestFocus() }.isSuccess) { pendingChIdx = -1; return@LaunchedEffect }
                delay(60)
            }
        }
        pendingChIdx = -1
        // 兜底：分组列选中 chip
        groupListState.scrollToItem(groupSel)
        repeat(10) {
            if (runCatching { cardReg.fr("g:$groupSel").requestFocus() }.isSuccess) return@LaunchedEffect
            delay(60)
        }
    }

    /* ---------- 分组浏览（聚焦即切换）时频道列回顶（不动焦点） ---------- */
    LaunchedEffect(groupSel) {
        if (!sidebarVisible || pendingChIdx >= 0) return@LaunchedEffect
        if (chListState.layoutInfo.totalItemsCount > 0) chListState.scrollToItem(0)
    }

    /* ---------- 起播失败自动跳台：12s 未恢复 → 下一台（连续 3 次上限防循环） ---------- */
    var autoSkips by remember { mutableIntStateOf(0) }
    LaunchedEffect(vmUi.playState) {
        if (vmUi.playState == EnginePlayState.READY) autoSkips = 0
    }
    LaunchedEffect(vmUi.fatalMsg, playingKey) {
        if (vmUi.fatalMsg == null) return@LaunchedEffect
        delay(12_000)
        if (vm.ui.value.fatalMsg == null) return@LaunchedEffect   // VM 30s 自动重试已恢复
        if (autoSkips >= 3) {
            hint = "多个频道播放失败，请手动选台"
            openSidebar()
            return@LaunchedEffect
        }
        autoSkips++
        hint = "该频道不可用，自动切换下一台…"
        skipChannel(+1)
    }

    /* ---------- 进页自动刷新（30min 节流）+ 驻留期每 60s 检查 ---------- */
    LaunchedEffect(Unit) {
        iptv.refreshIfStale()
        while (true) {
            delay(60_000)
            iptv.refreshIfStale()
        }
    }

    /* ---------- BACK（v1.20 电视#2 重分层）：
     * 浏览态 → 直接返回上一页；播放态侧边栏内逐层收起；播放态画面双击返回（提示
     * 「再按一次返回」，2s 窗口）——必达出口：任意状态连按两次 BACK 必离开本页 ---------- */
    /** 二级菜单（频道列）返回一级：滚回选中分组并落焦该 chip（与行内 LEFT 定向同款） */
    fun backToGroupColumn() {
        scope.launch {
            groupListState.scrollToItem(groupSel)
            repeat(10) {
                if (runCatching { cardReg.fr("g:$groupSel").requestFocus() }.isSuccess) return@launch
                delay(50)
            }
        }
    }

    BackHandler {
        when {
            // 浏览态：直接返回上一页（tab 栈自然回退）
            !playing -> nav.popBackStack()
            // 播放态侧边栏内：严格分层返回，不与「双击返回」抢判定
            sidebarVisible && OtvNav.currentGroup == "tv-channels" -> backToGroupColumn()
            sidebarVisible -> sidebarVisible = false
            // 播放态画面：2s 内两击返回上一页（必达出口）；呼出侧边栏走 OK/MENU/触屏
            android.os.SystemClock.uptimeMillis() - lastBackAt < 2000 -> {
                lastBackAt = 0L
                navigateToTab(nav, "live")
            }
            else -> {
                lastBackAt = android.os.SystemClock.uptimeMillis()
                hint = "再按一次返回"
            }
        }
    }

    /* ======================= 浏览态：下方两级频道菜单（电视#1） ======================= */
    if (!playing) {
        TvBrowseScreen(
            nav = nav, iptv = iptv, ui = ui, s = s, scope = scope,
            cardReg = cardReg,
            groupSel = groupSel, onGroupSel = { groupSel = it },
            hint = hint,
            onPlay = { g, ch -> startPlay(g, ch, keepSidebar = false) },
        )
        return
    }

    /* ======================= 播放态：全屏沉浸直播 ======================= */
    // 触屏：点按画面直接呼出侧边栏（需求⑩：不再有中间控件浮层）——挂在根 Box 上
    // 而不是叠一层 matchParentSize 的透明 Box：后者会盖住侧边栏行的点击。
    // 子控件（侧边栏行）先消费自己的点击，空白画面区冒泡到根。
    Box(
        Modifier
            .fillMaxSize()
            .background(Color.Black)
            .pointerInput(playing, sidebarVisible) {
                detectTapGestures {
                    if (playing && !sidebarVisible && ui.channels.isNotEmpty()) openSidebar()
                }
            },
    ) {

        /* ---------- 视频层：key=引擎实例代数（v1.16）。换台/备用线热切换复用同一引擎
            实例（prepare 同 Player 换源），渲染面不重建——旧画面停帧过渡到新首帧，无黑屏；
            仅引擎实例更换（VLC 回退等 epoch+1）才重建渲染节点（factory 摘旧 parent 防崩溃）---------- */
        if (vm.hasEngine()) {
            key("${vmUi.engineEpoch}") {
                AndroidView(
                    factory = {
                        // 换台时引擎实例与 key 同帧重建：renderView 仍挂在旧 AndroidViewHolder
                        // 上（旧节点本帧晚些才移除），直接 addView 会抛
                        // 「child already has a parent」——先从旧 parent 摘除再挂载。
                        // 三段回退切 libVLC 时是新实例新 view（无 parent），摘除为 no-op。
                        val v = vm.engine.renderView
                        (v.parent as? android.view.ViewGroup)?.removeView(v)
                        v
                    },
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }

        // 缓冲指示（换台/起播中）
        if (ui.channels.isNotEmpty() && vmUi.playState == EnginePlayState.BUFFERING && vmUi.fatalMsg == null) {
            AppleLoading(modifier = Modifier.align(Alignment.Center), size = 56.dp)
        }

        // 起播失败提示（轻量文字，非点播式错误弹层；自动跳台由上方 effect 接管）
        vmUi.fatalMsg?.let { msg ->
            Box(
                Modifier.align(Alignment.Center).background(Color(0xB3111114), RoundedCornerShape(16f.sx(s)))
                    .padding(horizontal = 40f.sx(s), vertical = 24f.sx(s)),
            ) {
                Text(msg, color = OtvColors.White75, fontSize = 26f.sxs(s))
            }
        }

        /* ---------- 左侧侧边栏：两级选台菜单（2026-09-05 需求⑧：贴屏幕左缘全高——
            左边与屏幕左侧重合、高度与屏幕一致，仅右侧保留圆角；一级分组 = tvOS 文字导航
            （浏览组白 8% 微底、焦点白底黑字），二级频道玻璃卡；列间结构分隔线 ---------- */
        if (sidebarVisible && ui.channels.isNotEmpty()) {
            Row(
                modifier = Modifier
                    .fillMaxHeight()
                    .width(900f.sx(s))
                    .background(Color(0xF00C0C10), RoundedCornerShape(topEnd = 28f.sx(s), bottomEnd = 28f.sx(s)))
                    .border(1.dp, Color(0x18FFFFFF), RoundedCornerShape(topEnd = 28f.sx(s), bottomEnd = 28f.sx(s)))
                    .padding(start = 26f.sx(s), end = 26f.sx(s), top = 30f.sx(s), bottom = 26f.sx(s)),
            ) {
                /* ----- 一级：分组纵向列（文字导航） ----- */
                Column(Modifier.width(230f.sx(s))) {
                    val groupScroll = remember(groupListState) { lazyNavContainer(groupListState) }
                    ProvideNavScrolls(vertical = groupScroll) {
                        LazyColumn(
                            state = groupListState,
                            verticalArrangement = Arrangement.spacedBy(10f.sx(s)),
                        ) {
                            items(ui.groups.size, key = { i -> "sg-$i" }) { i ->
                                val g = ui.groups[i]
                                val active = i == groupSel
                                var f by remember { mutableStateOf(false) }
                                Box(
                                    contentAlignment = Alignment.CenterStart,
                                    modifier = Modifier
                                        .dpadFocusable(
                                            scaleFocused = 1.02f,
                                            focusedBg = OtvColors.White,
                                            focusedBgRadius = 16f.sx(s),
                                            onFocusedChange = { f = it },
                                            autoSelect = true,
                                            navGroup = "tv-sidebar-groups",
                                            externalFocusRequester = cardReg.fr("g:$i"),
                                            navOverride = { dir ->
                                                when (dir) {
                                                    // 分组列 RIGHT：定向落焦频道列首行（几何搜索会撞本列）
                                                    NAV_RIGHT -> { pendingChIdx = -1; focusTick++; true }
                                                    else -> false
                                                }
                                            },
                                        ) { groupSel = i; hasBrowsed = true }
                                        .background(
                                            if (!f && active) Color(0x14FFFFFF) else Color.Transparent,
                                            RoundedCornerShape(16f.sx(s)),
                                        )
                                        .fillMaxWidth()
                                        .padding(horizontal = 20f.sx(s))
                                        .height(64f.sx(s)),
                                ) {
                                    Text(
                                        g,
                                        color = if (f) OtvColors.Bg else if (active) OtvColors.White else OtvColors.White60,
                                        fontSize = 22f.sxs(s),
                                        fontWeight = if (f) FontWeight.Bold else if (active) FontWeight.SemiBold else FontWeight.Medium,
                                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                                    )
                                }
                            }
                        }
                    }
                }

                Spacer(Modifier.width(16f.sx(s)))
                Box(Modifier.width(1.dp).fillMaxHeight().background(Color(0x14FFFFFF)))
                Spacer(Modifier.width(16f.sx(s)))

                /* ----- 二级：频道列表（玻璃卡，无标题） ----- */
                Column(
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxHeight(),
                ) {
                    val chScroll = remember(chListState) { lazyNavContainer(chListState) }
                    ProvideNavScrolls(vertical = chScroll) {
                        LazyColumn(
                            state = chListState,
                            verticalArrangement = Arrangement.spacedBy(13f.sx(s)),
                            modifier = Modifier.fillMaxWidth(),
                        ) {
                            itemsIndexed(groupChannels, key = { _, ch -> ch.group + "|" + ch.name }) { _, ch ->
                                ChannelRowCard(
                                    s = s,
                                    ch = ch,
                                    playing = ch.name == playingName && ch.group == playingGroup,
                                    handle = cardReg.fr(ch.group + "|" + ch.name),
                                    onFocus = { key -> lastChKey = key; hasBrowsed = true },
                                    onClick = { startPlay(ch.group, ch, keepSidebar = false) },
                                    navOverride = { dir ->
                                        when (dir) {
                                            // 频道列 LEFT：定向回选中分组 chip（几何会抢同行上邻）
                                            NAV_LEFT -> {
                                                scope.launch {
                                                    groupListState.scrollToItem(groupSel)
                                                    repeat(10) {
                                                        if (runCatching { cardReg.fr("g:$groupSel").requestFocus() }.isSuccess) return@launch
                                                        delay(50)
                                                    }
                                                }
                                                true
                                            }
                                            else -> false
                                        }
                                    },
                                )
                            }
                        }
                    }
                }
            }
        }

        /* ---------- 空/加载态（无数据时侧边栏不显示） ---------- */
        if (ui.channels.isEmpty()) {
            when {
                ui.loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { AppleLoading() }
                else -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    // v1.17：源失效走仓库级自动重试（60s 起指数退避），页面只给状态提示
                    Text(
                        (ui.lastResult ?: "直播源加载失败") + "\n正在自动重试，请稍候…",
                        color = OtvColors.White60, fontSize = 26f.sxs(s),
                        textAlign = androidx.compose.ui.text.style.TextAlign.Center,
                        lineHeight = 38f.sxs(s),
                    )
                }
            }
        }

        /* ---------- 画面态键宿（需求⑩ 2026-09-07）：LEFT/OK 直接呼出选台侧边栏
            （不再经过控件浮层），UP/DOWN 直接换台 ---------- */
        Box(
            Modifier
                .size(1.dp)
                .navSink(rootSink) { dir ->
                    when (dir) {
                        NAV_LEFT -> { openSidebar(); true }
                        NAV_UP -> { skipChannel(-1); true }
                        NAV_DOWN -> { skipChannel(+1); true }
                        else -> true   // RIGHT 封锁（画面无右缘内容）
                    }
                }
                .onPreviewKeyEvent { e ->
                    if (e.type != KeyEventType.KeyDown || e.nativeKeyEvent.repeatCount != 0) {
                        return@onPreviewKeyEvent false
                    }
                    when (e.nativeKeyEvent.keyCode) {
                        KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER -> {
                            openSidebar(); true   // 需求⑩：OK 直接弹选台侧边栏
                        }
                        KeyEvent.KEYCODE_MENU -> {
                            openSidebar(); true
                        }
                        else -> false
                    }
                },
        )

        OtvHint(hint)
    }
}

/**
 * 浏览态页面（v1.20 电视#1；2026-09-04 需求④⑥ + 深夜追加改版）：进入电视页先见
 * 频道菜单，不直接进直播。标题区已去除（需求④）；一级分组顶部一行（chips 横排，
 * hover-select 白底胶囊）+ 二级频道下方多行多列网格（LazyVerticalGrid 纵向滚动，
 * ChannelRowCard 台标行，聚焦白底反色）。OK 点击频道 → 进直播画面。
 * 焦点（OtvNav）：分组行内左右 hover-select 切分组；分组 DOWN 定向网格首卡、
 * UP 定向回「电视」tab；网格内几何移动（RowBandNav 按行带），首行 UP 几何落分组行。
 */
@Composable
private fun TvBrowseScreen(
    nav: NavController,
    iptv: IptvRepository,
    ui: IptvRepository.IptvState,
    s: Float,
    scope: kotlinx.coroutines.CoroutineScope,
    cardReg: FocusRegistry,
    groupSel: Int,
    onGroupSel: (Int) -> Unit,
    hint: String?,
    onPlay: (String, IptvRepository.Channel) -> Unit,
) {
    val groups = ui.groups
    val selGroup = groups.getOrNull(groupSel) ?: ""
    val groupChannels = remember(ui.channels, selGroup) { ui.channels.filter { it.group == selGroup } }

    val tabFocus = remember { FocusRequester() }
    val firstChipFocus = remember { FocusRequester() }
    // 需求 电视#8：hover 到「电视」tab 切页后光标停在 tab 上（与足球/影视/我的页一致），
    // 不再自动落进下方分组 chips；tab DOWN 仍经 contentDownFocus 定向进首 chip
    InitialFocusEffect(tabFocus, "tv-tab", enabled = true)

    // 浏览态分组行内持焦守卫（与影视 chips 同款 gen 代际防抖）
    var chipRowHasFocus by remember { mutableStateOf(false) }
    var chipRowGen by remember { mutableIntStateOf(0) }

    val chipRowState = rememberLazyListState()
    val gridState = rememberLazyGridState()

    // 分组切换时频道网格回顶
    LaunchedEffect(groupSel) {
        if (gridState.layoutInfo.totalItemsCount > 0) gridState.scrollToItem(0)
    }

    Box(Modifier.fillMaxSize().background(OtvColors.Bg)) {

        /* ---------- 空态 ---------- */
        if (ui.channels.isEmpty()) {
            when {
                ui.loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { AppleLoading() }
                else -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text(
                        (ui.lastResult ?: "直播源加载失败") + "\n正在自动重试，请稍候…",
                        color = OtvColors.White60, fontSize = 26f.sxs(s),
                        textAlign = androidx.compose.ui.text.style.TextAlign.Center,
                        lineHeight = 38f.sxs(s),
                    )
                }
            }
        }

        /* ---------- 两级选台菜单（一级顶部一行 + 二级网格纵向滚动） ---------- */
        if (ui.channels.isNotEmpty()) {
            Column(
                Modifier
                    .fillMaxSize()
                    .padding(top = 150f.sx(s), start = 76f.sx(s), end = 76f.sx(s), bottom = 50f.sx(s)),
            ) {
                /* ----- 一级：分组 chips 顶部一行（hover-select：行内移动即切换） ----- */
                val chipScroll = remember(chipRowState) { lazyNavContainer(chipRowState) }
                ProvideNavScrolls(horizontal = chipScroll) {
                    LazyRow(
                        state = chipRowState,
                        horizontalArrangement = Arrangement.spacedBy(18f.sx(s)),
                        contentPadding = PaddingValues(end = 40f.sx(s)),
                    ) {
                        items(groups.size, key = { i -> "bg-$i" }) { i ->
                            val g = groups[i]
                            val active = i == groupSel
                            var chipFocused by remember { mutableStateOf(false) }
                            Box(
                                contentAlignment = Alignment.Center,
                                modifier = Modifier
                                    .dpadFocusable(
                                        scaleFocused = 1.05f, focusedBg = OtvColors.White,
                                        focusedBgRadius = 32f.sx(s),
                                        onFocusedChange = { gained ->
                                            chipFocused = gained
                                            if (gained) {
                                                chipRowGen++
                                                val fromInside = chipRowHasFocus
                                                chipRowHasFocus = true
                                                if (fromInside && !active) onGroupSel(i)
                                            } else {
                                                val gen = ++chipRowGen
                                                scope.launch {
                                                    delay(150)
                                                    if (chipRowGen == gen) chipRowHasFocus = false
                                                }
                                            }
                                        },
                                        externalFocusRequester = if (i == 0) firstChipFocus else cardReg.fr("bg:$i"),
                                        navGroup = "tv-browse-groups",
                                        navOverride = { dir ->
                                            when (dir) {
                                                // 分组行 DOWN：定向落当前分组网格首卡（几何会撞 chips 本行右侧）
                                                NAV_DOWN -> {
                                                    scope.launch {
                                                        repeat(10) {
                                                            val first = groupChannels.firstOrNull() ?: return@launch
                                                            if (runCatching { cardReg.fr("bc:" + first.group + "|" + first.name).requestFocus() }.isSuccess) {
                                                                return@launch
                                                            }
                                                            delay(60)
                                                        }
                                                    }
                                                    true
                                                }
                                                NAV_UP -> {
                                                    // 需求 电视#8：显式定向回「电视」tab（光标回到标签栏可继续切页）
                                                    scope.launch {
                                                        repeat(10) {
                                                            if (runCatching { tabFocus.requestFocus() }.isSuccess) return@launch
                                                            delay(50)
                                                        }
                                                    }
                                                    true
                                                }   // 顶栏之上封锁（UP 到顶由引擎 topFocus 回 tab）
                                                else -> false
                                            }
                                        },
                                    ) { onGroupSel(i) }
                                    .background(
                                        if (chipFocused) OtvColors.White else OtvColors.ChipBg,
                                        RoundedCornerShape(32f.sx(s)),
                                    )
                                    .padding(horizontal = 34f.sx(s))
                                    .height(64f.sx(s)),
                            ) {
                                Text(
                                    g,
                                    color = if (chipFocused) OtvColors.Bg else if (active) OtvColors.White else OtvColors.White.copy(alpha = 0.6f),
                                    fontSize = 29f.sxs(s), fontWeight = FontWeight.Medium,
                                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                                )
                            }
                        }
                    }
                }

                Spacer(Modifier.height(26f.sx(s)))

                /* ----- 二级：频道网格（多行多列，纵向滚动；网格内几何移动） ----- */
                val gridScroll = remember(gridState) { lazyGridNavContainer(gridState) }
                ProvideNavScrolls(vertical = gridScroll) {
                    LazyVerticalGrid(
                        state = gridState,
                        columns = GridCells.Fixed(4),
                        horizontalArrangement = Arrangement.spacedBy(22f.sx(s)),
                        verticalArrangement = Arrangement.spacedBy(18f.sx(s)),
                        modifier = Modifier.weight(1f).fillMaxWidth(),
                    ) {
                        items(groupChannels.size, key = { i -> "bc-" + groupChannels[i].group + "|" + groupChannels[i].name }) { i ->
                            val ch = groupChannels[i]
                            ChannelRowCard(
                                s = s,
                                ch = ch,
                                playing = false,
                                handle = cardReg.fr("bc:" + ch.group + "|" + ch.name),
                                onFocus = { },
                                onClick = { onPlay(ch.group, ch) },
                                navOverride = { dir ->
                                    // 引擎垂直搜索限定同 navGroup（tv-channels），跨区到分组行
                                    // （tv-browse-groups）必须显式定向（2026-09-05 用户反馈：
                                    // 光标无法在 标签栏/一级/二级 间来回）
                                    when {
                                        // 首行 UP：定向回选中分组 chip
                                        dir == NAV_UP && i < 4 -> {
                                            scope.launch {
                                                chipRowState.scrollToItem(groupSel)
                                                repeat(10) {
                                                    val fr = if (groupSel == 0) firstChipFocus else cardReg.fr("bg:$groupSel")
                                                    if (runCatching { fr.requestFocus() }.isSuccess) return@launch
                                                    delay(60)
                                                }
                                            }
                                            true
                                        }
                                        // 首列 LEFT：定向回选中分组 chip
                                        dir == NAV_LEFT && i % 4 == 0 -> {
                                            scope.launch {
                                                chipRowState.scrollToItem(groupSel)
                                                repeat(10) {
                                                    val fr = if (groupSel == 0) firstChipFocus else cardReg.fr("bg:$groupSel")
                                                    if (runCatching { fr.requestFocus() }.isSuccess) return@launch
                                                    delay(60)
                                                }
                                            }
                                            true
                                        }
                                        else -> false   // 网格内几何移动（同组）
                                    }
                                },
                            )
                        }
                    }
                }
            }
        }

        // 顶栏 tab（浏览态恢复，与其他主页面一致；DOWN 定向进内容首焦点=首个分组 chip）
        // 2026-09-19：补 autoHide——光标下移进频道网格时顶栏隐藏、回顶部显示
        //（此前只有影视/足球/我的/搜索开了，电视页漏掉，用户报「下移时顶栏不隐藏」）
        com.qiubo.optimaltv.ui.components.MainTabBar(
            "tv", { key -> navigateToTab(nav, key) }, { nav.navigate("search") }, tabFocus,
            contentDownFocus = firstChipFocus,
            contentDownScrollTop = { },
            autoHide = true,   // 光标下移进内容区隐藏顶栏，回顶部显示
        )

        OtvHint(hint)
    }
}

/**
 * 侧边栏频道行：台标（Coil 管线，失败回退双色字母占位）+ 频道名；
 * 当前播放项微亮底 + 「正在播放」标记（Accent 色）。
 */
@Composable
private fun ChannelRow(
    s: Float,
    ch: IptvRepository.Channel,
    playing: Boolean,
    handle: FocusRequester,
    onFocus: (String) -> Unit,
    onClick: () -> Unit,
    navOverride: (Int) -> Boolean,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .height(96f.sx(s))
            .dpadFocusable(
                scaleFocused = 1.03f,
                onFocusedChange = { focused ->
                    if (focused) {
                        OtvLog.i("tv focus: ${ch.name}")
                        onFocus(ch.group + "|" + ch.name)
                    }
                },
                externalFocusRequester = handle,
                navGroup = "tv-channels",   // 与分组列组隔离：UP/DOWN 组内几何+本列滚动兜底
                navOverride = navOverride,
                onClick = onClick,
            )
            .background(
                if (playing) Color(0x2EFFFFFF) else OtvColors.Panel,
                RoundedCornerShape(14f.sx(s)),
            )
            .padding(horizontal = 18f.sx(s)),
    ) {
        Box(
            Modifier
                .size(58f.sx(s))
                .clip(RoundedCornerShape(10f.sx(s))),
        ) {
            if (ch.logo.isNotBlank()) {
                AsyncImage(
                    model = ch.logo,
                    contentDescription = ch.name,
                    contentScale = ContentScale.Fit,
                    modifier = Modifier.fillMaxSize(),
                )
            } else {
                LogoPlaceholder(ch.name)
            }
        }
        Spacer(Modifier.width(18f.sx(s)))
        Text(
            ch.name,
            color = if (playing) OtvColors.White else OtvColors.White75,
            fontSize = 24f.sxs(s),
            fontWeight = if (playing) FontWeight.SemiBold else FontWeight.Normal,
            maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f, fill = false),
        )
        if (playing) {
            Spacer(Modifier.weight(1f))
            Text("正在播放", color = AccentBlue, fontSize = 17f.sxs(s), fontWeight = FontWeight.SemiBold)
        }
    }
}

/**
 * 播放态频道行（卡片式）：台标 + 频道名；当前播放项微亮底 + 「正在播放」标记；
 * 聚焦白底反色放大（Apple TV 风格）
 */
@Composable
private fun ChannelRowCard(
    s: Float,
    ch: IptvRepository.Channel,
    playing: Boolean,
    handle: FocusRequester,
    onFocus: (String) -> Unit,
    onClick: () -> Unit,
    navOverride: (Int) -> Boolean,
) {
    var f by remember { mutableStateOf(false) }
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .height(100f.sx(s))
            .dpadFocusable(
                scaleFocused = if (f) 1.03f else 1.0f,
                focusedBg = Color.Transparent,
                focusedBgRadius = 20f.sx(s),
                onFocusedChange = { f = it; if (it) onFocus(ch.group + "|" + ch.name) },
                externalFocusRequester = handle,
                navGroup = "tv-channels",
                navOverride = navOverride,
                onClick = onClick,
            )
            .background(
                // 聚焦=深底微亮+白字+引擎白环；播放=蓝 16% 底+蓝 hairline；常态=玻璃白 5.5%
                // （深底聚焦是硬约束——白色剪影台标在白底上会消失）
                when {
                    f -> Color(0xFF2E2E3A)
                    playing -> AccentBlue.copy(alpha = 0.16f)
                    else -> Color(0x0EFFFFFF)
                },
                RoundedCornerShape(20f.sx(s)),
            )
            .border(
                1.dp,
                when {
                    playing -> AccentBlue.copy(alpha = 0.35f)
                    f -> Color.Transparent
                    else -> Color(0x0DFFFFFF)
                },
                RoundedCornerShape(20f.sx(s)),
            )
            .padding(horizontal = 26f.sx(s)),
    ) {
        Box(
            Modifier
                .size(70f.sx(s))
                .clip(RoundedCornerShape(16f.sx(s)))
                .background(Color(0x0FFFFFFF)),
        ) {
            if (ch.logo.isNotBlank()) {
                AsyncImage(
                    model = ch.logo,
                    contentDescription = ch.name,
                    contentScale = ContentScale.Fit,
                    modifier = Modifier.fillMaxSize(),
                )
            } else {
                LogoPlaceholder(ch.name)
            }
        }
        Spacer(Modifier.width(24f.sx(s)))
        Text(
            ch.name,
            color = if (f) OtvColors.White else if (playing) OtvColors.White75 else OtvColors.White,
            fontSize = 27f.sxs(s),
            fontWeight = if (f) FontWeight.SemiBold else FontWeight.Normal,
            maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f, fill = false),
        )
        if (playing) {
            // 「正在播放」标记（▶）：仅当前播放频道显示；聚焦态不再画蓝圆（2026-09-05 反馈）
            Spacer(Modifier.width(12f.sx(s)))
            Box(
                Modifier
                    .size(24f.sx(s))
                    .clip(CircleShape)
                    .background(AccentBlue),
                contentAlignment = Alignment.Center,
            ) {
                Text("▶", color = OtvColors.Bg, fontSize = 14f.sxs(s))
            }
        }
    }
}
@Composable
private fun LogoPlaceholder(name: String) {
    val s = rememberUiScale()
    val palettes = listOf(
        listOf(Color(0xFF2E4A6B), Color(0xFF121E2E)),
        listOf(Color(0xFF50345C), Color(0xFF1A1220)),
        listOf(Color(0xFF2A5248), Color(0xFF101E19)),
        listOf(Color(0xFF5C4630), Color(0xFF201710)),
    )
    val pal = palettes[Math.floorMod(name.hashCode(), palettes.size)]
    Box(
        Modifier
            .fillMaxSize()
            .background(Brush.linearGradient(pal)),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            name.take(2),
            color = OtvColors.White75,
            fontSize = 26f.sxs(s),
            fontWeight = FontWeight.Bold,
        )
    }
}


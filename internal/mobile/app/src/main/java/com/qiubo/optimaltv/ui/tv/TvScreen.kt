package com.qiubo.optimaltv.ui.tv

import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import coil.compose.AsyncImage
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.OtvLog
import com.qiubo.optimaltv.R
import com.qiubo.optimaltv.data.repo.IptvRepository
import com.qiubo.optimaltv.data.repo.LineSpeed
import com.qiubo.optimaltv.playback.EnginePlayState
import com.qiubo.optimaltv.ui.components.AppleLoading
import com.qiubo.optimaltv.ui.components.OtvHint
import com.qiubo.optimaltv.ui.components.tapCard
import com.qiubo.optimaltv.ui.components.MobileBackButton
import com.qiubo.optimaltv.ui.components.isScrolledFromTop
import com.qiubo.optimaltv.ui.components.navigateToTab
import com.qiubo.optimaltv.ui.detail.simpleFactory
import com.qiubo.optimaltv.ui.player.PlayerViewModel
import com.qiubo.optimaltv.ui.theme.AccentBlue
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.launch

/** 遥控器/键盘「确定」键事件总线：MainActivity.onKeyDown 转发 →
 *  电视页播放态据此弹出选台竖条与投屏胶囊（盒子/投影仪遥控用户入口） */
object OkKeyBus {
    val pokes = MutableSharedFlow<Unit>(extraBufferCapacity = 8)
    fun poke() {
        pokes.tryEmit(Unit)
    }
}

/**
 * 电视页（v1.20 交互重做；2026-09-04 需求④⑥ 调整）：
 * - 浏览态（进入默认，电视#1）：不直接进直播——页面即两列选台器（一级分组纵向列 +
 *   二级频道纵向列表，需求⑥ 换回原样式；标题区已按需求④ 去除），点选频道才进直播；
 *   顶栏 tab 可切页。
 * - 播放态（选中频道后）：全屏沉浸直播；三枚悬浮控制键 = 左上「返回」+ 右上「投屏」+
 *   左缘「选台」把手（2026-09-11 交互统一：点按画面一下，三键显↔隐整体切换，无自动隐藏），
 *   点选台把手呼出两列侧边栏（一级分组列 + 二级频道列，与浏览态同构）；
 *   侧边栏开时点空白处关闭；BACK / 左上返回键一次回浏览态（总体#5）。
 * - 电视#5 实时测速：换台按测速分数选最快线路、起播 3.5s 仍缓冲且测出明显更快
 *   线路时无感热切换（每频道一次）；驻留期每 60s 轮询当前分组线路分数；
 * - 电视#7：侧边栏记住上次浏览的分组（rememberSaveable，不拽回播放分组）。
 *
 * 健壮性与播放链路与 TV 版一致：bootIptv 全链路（UrlGuard/看门狗/三段回退）、
 * 起播失败 12s 自动跳台（3 次上限）、KEEP_SCREEN_ON、
 * 后台暂停/回来自动恢复。竖屏（总体#2）：侧边栏宽度上限 86% 屏宽、菜单字号自适应。
 */
@Composable
fun TvScreen(nav: NavController) {
    val iptv = Graph.iptv
    val ui by iptv.state.collectAsStateWithLifecycle()
    val s = rememberUiScale()
    val scope = rememberCoroutineScope()
    val cfg = LocalConfiguration.current
    val portrait = cfg.orientation == android.content.res.Configuration.ORIENTATION_PORTRAIT

    /* ---------- 播放会话：页面内嵌 PlayerViewModel（数据就绪后显式起播） ---------- */
    val vm: PlayerViewModel = viewModel(
        key = "tv-iptv",
        factory = simpleFactory {
            PlayerViewModel("iptv:", 0, Graph.repo, Graph.db, Graph.settings, autoBoot = false)
        },
    )
    val vmUi by vm.ui.collectAsStateWithLifecycle()

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

    /* ---------- 当前播放频道（侧边栏高亮基准） ---------- */
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
    var groupSel by rememberSaveable(ui.groups) { mutableIntStateOf(0) }   // 电视#7：记住上次浏览分组
    val selGroup = ui.groups.getOrNull(groupSel) ?: ""
    val groupChannels = remember(ui.channels, selGroup) { ui.channels.filter { it.group == selGroup } }

    var hint by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(hint) { if (hint != null) { delay(2500); hint = null } }

    val groupListState = rememberLazyListState()
    val chListState = rememberLazyListState()

    /* ---------- 2026-09-11 三键统一开关：左上返回 / 右上投屏 / 左缘选台把手同进同出，
        点按画面一下切换显隐（无自动隐藏计时器） ---------- */
    var controlsVisible by remember { mutableStateOf(false) }
    // 遥控器/键盘「确定」：三键未显示 → 先显示；已显示 → 直接打开选台侧边栏
    //（盒子/投影仪遥控用户全流程可达：OK OK → 侧边栏 → 方向键选台 → OK 播放；
    //  竖屏列表常驻，OK 无操作）
    LaunchedEffect(Unit) {
        OkKeyBus.pokes.collect {
            if (portrait || !playing || ui.channels.isEmpty() || sidebarVisible) return@collect
            if (controlsVisible) sidebarVisible = true else controlsVisible = true
        }
    }

    /* ---------- 换台动作（写记忆 + bootIptv 全链路；电视#5 测速选线） ---------- */
    fun startPlay(group: String, ch: IptvRepository.Channel, lineIdx: Int = 0) {
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
        controlsVisible = true   // 进播放先亮出三键（返回/投屏/选台），点画面可整体隐藏
        hint = ch.name   // 换台反馈：底部轻提示频道名
        scope.launch { runCatching { Graph.settings.rememberIptvLast(group, ch.name, url) } }
        scope.launch { iptv.probeAll(ch.urls) }   // 电视#5：后台补测本频道全部线路
        vm.switchIptv(ch.name, url, urls.filterIndexed { i, _ -> i != urlIdx })
        sidebarVisible = false   // 触控语义：选台后收起侧边栏，全屏观看
    }

    // 分组切换时频道列回顶
    LaunchedEffect(groupSel) {
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
            sidebarVisible = true
            return@LaunchedEffect
        }
        autoSkips++
        hint = "该频道不可用，自动切换下一台…"
        val all = ui.channels
        if (all.isNotEmpty()) {
            val idx = all.indexOfFirst { it.name == playingName && it.group == playingGroup }
            val ch = all[if (idx < 0) 0 else (idx + 1).mod(all.size)]
            startPlay(ch.group, ch)
        }
    }

    /* ---------- 电视#5：起播慢时自动切最快线路（3.5s 仍缓冲 + 明显更快线） ---------- */
    LaunchedEffect(playingKey, vmUi.playState) {
        if (autoSwapped || vmUi.playState != EnginePlayState.BUFFERING || vmUi.fatalMsg != null) return@LaunchedEffect
        kotlinx.coroutines.delay(3_500)
        if (autoSwapped || vm.ui.value.playState != EnginePlayState.BUFFERING ||
            vm.ui.value.fatalMsg != null || vm.ui.value.isPlaying
        ) return@LaunchedEffect
        val ch = ui.channels.firstOrNull { it.name == playingName && it.group == playingGroup } ?: return@LaunchedEffect
        val cur = playingLineScore
        val best = iptv.rankUrls(ch.urls).firstOrNull {
            it != playingUrl && LineSpeed.scoreOf(it)?.let { sc -> sc < LineSpeed.FAIL_MS } == true
        }
        val bestScore = best?.let { LineSpeed.scoreOf(it) }
        if (best != null && bestScore != null &&
            (cur == null || bestScore < cur * 0.55f)
        ) {
            autoSwapped = true
            OtvLog.i("tv 测速自动换线: ${playingName} ${cur ?: -1}ms → ${bestScore}ms")
            hint = "已自动切换最快线路"
            vm.switchIptv(ch.name, best, ch.urls.filter { it != playingUrl && it != best })
        }
    }

    /* ---------- 电视#5：驻留期轮询测速（当前分组线路，滚动刷新分数） ---------- */
    LaunchedEffect(selGroup, ui.channels) {
        while (true) {
            val targets = ui.channels.filter { it.group == selGroup }.flatMap { it.urls }
            if (targets.isNotEmpty()) iptv.probeAll(targets.take(40))
            kotlinx.coroutines.delay(60_000)
        }
    }

    /* ---------- 进页自动刷新（30min 节流）+ 驻留期每 60s 检查 ---------- */
    LaunchedEffect(Unit) {
        iptv.refreshIfStale()
        while (true) {
            delay(60_000)
            iptv.refreshIfStale()
        }
    }

    /* ---------- BACK（总体#5 一次返回）：浏览态/播放态侧边栏开→逐层收；其余直接退出 ---------- */
    BackHandler {
        when {
            playing && sidebarVisible -> sidebarVisible = false
            playing -> playing = false   // 播放态返回先回频道浏览（电视#5 浏览优先）
            else -> nav.popBackStack()
        }
    }

    /* ======================= 浏览态：分组顶行 + 频道网格（电视#1 + 需求④⑥） ======================= */
    if (!playing) {
        TvBrowseScreen(
            nav = nav, ui = ui, s = s, portrait = portrait,
            groupSel = groupSel, onGroupSel = { groupSel = it },
            hint = hint,
            onPlay = { g, ch -> startPlay(g, ch) },
        )
        return
    }

    /* ======================= 播放态：全屏沉浸直播 =======================
       需求12⑧（2026-09-07 竖屏重构）：竖屏=顶部 16:9 视频区 + 下方常驻频道列表
       （常规竖屏电视直播 App 形态，无 FAB/浮层）；横屏保持全屏沉浸 + 悬浮侧边栏 */

    // 侧边栏宽度：横屏与 TV 版同款 980 设计px；竖屏上限 86% 屏宽（总体#2）
    val sidebarWidthDp = if (portrait) (cfg.screenWidthDp * 0.86f).dp else 980f.sx(s)

    // 侧边栏内容（横屏=左缘悬浮浮层；竖屏=视频下方内联块，需求12⑧）
        /* ---------- 左侧侧边栏：两级选台菜单（2026-09-05 需求⑧：贴屏幕左缘全高——
            左边与屏幕左侧重合、高度与屏幕一致，仅右侧保留圆角；
            一级分组纵向列 + 二级频道纵向列，v1.15-1.19 同构）
            需求12⑧：竖屏改为视频下方常驻内联块（同一内容，非浮层） ---------- */
        val sidebarContent: @Composable (Modifier) -> Unit = { m ->
            Row(modifier = m) {
                /* ----- 一级：分组纵向列 ----- */
                LazyColumn(
                    state = groupListState,
                    verticalArrangement = Arrangement.spacedBy(12f.sx(s)),
                    modifier = Modifier.width(if (portrait) sidebarWidthDp * 0.34f else 260f.sx(s)),
                ) {
                    items(ui.groups.size, key = { i -> "sg-$i" }) { i ->
                        TvGroupChip(
                            text = ui.groups[i],
                            active = i == groupSel,
                            accent = true,
                            s = s, portrait = portrait,
                        ) { groupSel = i }
                    }
                }

                Spacer(Modifier.width(16f.sx(s)))
                Box(Modifier.width(1.dp).fillMaxHeight().background(Color(0x14FFFFFF)))
                Spacer(Modifier.width(16f.sx(s)))

                /* ----- 二级：频道列表（纵向整宽卡片式，2026-09-05 去标题） ----- */
                Column(
                    Modifier.weight(1f).fillMaxHeight(),
                ) {
                    LazyColumn(
                        state = chListState,
                        verticalArrangement = Arrangement.spacedBy(12f.sx(s)),
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        itemsIndexed(groupChannels, key = { _, ch -> ch.group + "|" + ch.name }) { _, ch ->
                            ChannelRow(
                                s = s,
                                portrait = portrait,
                                ch = ch,
                                playing = ch.name == playingName && ch.group == playingGroup,
                                onClick = { startPlay(ch.group, ch) },
                            )
                        }
                    }
                }
            }
        }

    androidx.compose.foundation.layout.Column(Modifier.fillMaxSize().background(Color.Black)) {
    Box(
        Modifier
            .fillMaxWidth()
            .then(if (portrait) Modifier.aspectRatio(16f / 9f) else Modifier.weight(1f)),
    ) {

        /* ---------- 视频层：key=引擎实例代数。换台/备用线热切换复用同一引擎实例，
            渲染面不重建；仅引擎实例更换（VLC 回退等 epoch+1）才重建渲染节点 ---------- */
        if (vm.hasEngine()) {
            key("${vmUi.engineEpoch}") {
                AndroidView(
                    factory = {
                        // 换台时引擎实例与 key 同帧重建：renderView 仍挂在旧 AndroidViewHolder
                        // 上，直接 addView 会抛「child already has a parent」——先摘除再挂载
                        val v = vm.engine.renderView
                        (v.parent as? android.view.ViewGroup)?.removeView(v)
                        // 同 PlayerScreen：Media3 PlayerView 构造时无条件 setOnClickListener
                        //（自身变 clickable）会吃掉全部触摸——空白点按关侧边栏收不到事件
                        (v as? androidx.media3.ui.PlayerView)?.apply {
                            useController = false
                            isClickable = false
                            isLongClickable = false
                        }
                        v
                    },
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }

        // 2026-09-11：点画面一下 = 三枚控制键（返回/投屏/选台）显隐整体切换；
        // 侧边栏打开时点空白先关闭侧边栏（竖屏同样作用于视频区上的返回/投屏两键）
        if (ui.channels.isNotEmpty()) {
            Box(
                Modifier
                    .fillMaxSize()
                    .pointerInput(sidebarVisible, portrait) {
                        detectTapGestures {
                            if (sidebarVisible) sidebarVisible = false
                            else controlsVisible = !controlsVisible
                        }
                    },
            )
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

        /* ---------- v1.21 投屏：Cast 入口（2026-09-11 移至右上角 + 跟随三键显隐） ---------- */
        var castDlg by remember { mutableStateOf(false) }
        if (castDlg) {
            LaunchedEffect(Unit) { vm.scanCastDevices() }
            androidx.compose.material3.AlertDialog(
                onDismissRequest = { castDlg = false },
                containerColor = Color(0xE9121216),
                titleContentColor = Color.White,
                textContentColor = OtvColors.White75,
                title = { Text(if (vmUi.castDevice != null) "投屏中" else "投屏到设备") },
                text = {
                    Column(verticalArrangement = Arrangement.spacedBy(12f.sx(s))) {
                        when {
                            vmUi.castScanning -> Text("正在扫描局域网设备…")
                            vmUi.castDevices.isEmpty() && vmUi.castDevice == null ->
                                Text("未发现设备（电视/盒子需与本机同一 Wi-Fi）")
                        }
                        vmUi.castDevice?.let { dev ->
                            Row(
                                Modifier
                                    .fillMaxWidth()
                                    .clip(RoundedCornerShape(12f.sx(s)))
                                    .background(Color(0x240A84FF))
                                    .clickable {
                                        vm.stopCast(resumeLocal = true)
                                        castDlg = false
                                    }
                                    .padding(horizontal = 20f.sx(s), vertical = 14f.sx(s)),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Text(
                                    "正在投屏 · ${dev.name}",
                                    color = Color(0xFF0A84FF),
                                    fontSize = 26f.sxs(s),
                                    modifier = Modifier.weight(1f),
                                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                                )
                                Text("断开", color = OtvColors.White75, fontSize = 24f.sxs(s))
                            }
                        }
                        vmUi.castDevices
                            .filter { it.uuid != vmUi.castDevice?.uuid }
                            .forEach { dev ->
                                Row(
                                    Modifier
                                        .fillMaxWidth()
                                        .clip(RoundedCornerShape(12f.sx(s)))
                                        .background(Color(0x24FFFFFF))
                                        .clickable {
                                            vm.startCast(dev)
                                            castDlg = false
                                        }
                                        .padding(horizontal = 20f.sx(s), vertical = 14f.sx(s)),
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    Text(
                                        dev.name, color = Color.White, fontSize = 26f.sxs(s),
                                        modifier = Modifier.weight(1f),
                                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                                    )
                                    Text("投屏", color = OtvColors.White75, fontSize = 24f.sxs(s))
                                }
                            }
                        vmUi.castError?.let { Text(it, color = Color(0xFFFFB84D), fontSize = 24f.sxs(s)) }
                    }
                },
                confirmButton = {
                    androidx.compose.material3.TextButton(onClick = { vm.scanCastDevices() }) {
                        Text("重新扫描", color = Color.White)
                    }
                },
                dismissButton = {
                    androidx.compose.material3.TextButton(onClick = { castDlg = false }) {
                        Text("关闭", color = OtvColors.White50)
                    }
                },
            )
        }
        androidx.compose.animation.AnimatedVisibility(
            visible = controlsVisible,
            enter = fadeIn(animationSpec = tween(180)),
            exit = fadeOut(animationSpec = tween(150)),
            modifier = Modifier.align(Alignment.TopEnd),
        ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier
                .statusBarsPadding()
                .padding(end = 26f.sx(s), top = 26f.sx(s))
                .clip(RoundedCornerShape(50))
                .background(
                    if (vmUi.castDevice != null) AccentBlue.copy(alpha = 0.28f) else Color(0xC714141A),
                )
                .border(
                    1.dp,
                    if (vmUi.castDevice != null) AccentBlue.copy(alpha = 0.55f) else Color(0x29FFFFFF),
                    RoundedCornerShape(50),
                )
                .clickable { castDlg = true }
                .padding(horizontal = 26f.sx(s), vertical = 13f.sx(s)),
        ) {
            if (vmUi.castDevice != null) {
                // 投屏中：绿点 + 蓝调胶囊 + 设备名常显
                Box(Modifier.size(12f.sx(s)).background(Color(0xFF30D158), CircleShape))
                Spacer(Modifier.width(12f.sx(s)))
                Text("投屏中", color = Color.White, fontSize = 26f.sxs(s), fontWeight = FontWeight.SemiBold, maxLines = 1)
                Spacer(Modifier.width(14f.sx(s)))
                Text(vmUi.castDevice?.name ?: "", color = OtvColors.White70, fontSize = 24f.sxs(s), maxLines = 1)
            } else {
                CastGlyph(30f.sx(s), Color.White)
                Spacer(Modifier.width(12f.sx(s)))
                Text("投屏", color = Color.White, fontSize = 26f.sxs(s), fontWeight = FontWeight.Medium)
            }
        }
        } // AnimatedVisibility(投屏胶囊)


        // 横屏：悬浮侧边栏浮层（贴屏幕左缘全高，仅右侧圆角）；竖屏走 Column 尾部的内联块
        if (!portrait && sidebarVisible && ui.channels.isNotEmpty()) {
            sidebarContent(
                Modifier
                    .fillMaxHeight()
                    .width(sidebarWidthDp)
                    .background(Color(0xF00C0C10), RoundedCornerShape(topEnd = 28f.sx(s), bottomEnd = 28f.sx(s)))
                    .border(1.dp, Color(0x18FFFFFF), RoundedCornerShape(topEnd = 28f.sx(s), bottomEnd = 28f.sx(s)))
                    .padding(start = 26f.sx(s), end = 26f.sx(s), top = 30f.sx(s), bottom = 26f.sx(s)),
            )
        }

        /* ---------- 空态（无数据时侧边栏不显示，把手不显示） ---------- */
        if (ui.channels.isEmpty()) {
            when {
                ui.loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { AppleLoading() }
                else -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    // 源失效走仓库级自动重试（60s 起指数退避），页面只给状态提示
                    Text(
                        (ui.lastResult ?: "直播源加载失败") + "\n正在自动重试，请稍候…",
                        color = OtvColors.White60, fontSize = 26f.sxs(s),
                        textAlign = androidx.compose.ui.text.style.TextAlign.Center,
                        lineHeight = 38f.sxs(s),
                    )
                }
            }
        }

        /* ---------- 「选台」左缘把手（2026-09-11 归入三键统一开关）----------
         * 56dp 圆形毛玻璃深底 + hairline 描边 + 双层投影的贴缘窄竖条（图标 + 竖排「选台」）；
         * 跟随点按画面整体显隐（无自动隐藏），点按呼出两级侧边栏
         * 需求12⑧：竖屏列表常驻，把手不显示 ---------- */
        androidx.compose.animation.AnimatedVisibility(
            visible = !portrait && !sidebarVisible && controlsVisible && ui.channels.isNotEmpty(),
            enter = fadeIn() + scaleIn(initialScale = 0.7f, animationSpec = tween(180)),
            exit = fadeOut() + scaleOut(targetScale = 0.7f, animationSpec = tween(150)),
        ) {
            /* 2026-09-05 视觉稿定稿：左缘贴边窄竖条（图标 + 竖排「选台」，sx 设计坐标）；
               外层热区右侧加宽 90sx 方便拇指命中 */
            Box(
                contentAlignment = Alignment.CenterStart,
                modifier = Modifier
                    .padding(top = (cfg.screenHeightDp * 0.42f).dp)
                    .size(width = 166f.sx(s), height = 250f.sx(s))
                    .tapCard { sidebarVisible = true; hint = null },
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier
                        .fillMaxHeight()
                        .width(76f.sx(s))
                        .clip(RoundedCornerShape(topEnd = 22f.sx(s), bottomEnd = 22f.sx(s)))
                        .background(Color(0xC714141A))
                        .border(1.dp, Color(0x29FFFFFF), RoundedCornerShape(topEnd = 22f.sx(s), bottomEnd = 22f.sx(s))),
                ) {
                    Column(
                        horizontalAlignment = Alignment.CenterHorizontally,
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(vertical = 18f.sx(s)),
                    ) {
                        Icon(
                            painter = painterResource(R.drawable.ic_channel_list),
                            contentDescription = "选台",
                            tint = Color.White,
                            modifier = Modifier.size(40f.sx(s)),
                        )
                        Spacer(Modifier.height(18f.sx(s)))
                        Text(
                            "选\n台",
                            color = Color.White,
                            fontSize = 30f.sxs(s),
                            fontWeight = FontWeight.SemiBold,
                            letterSpacing = 6f.sxs(s),
                            textAlign = androidx.compose.ui.text.style.TextAlign.Center,
                            lineHeight = 38f.sxs(s),
                        )
                    }
                }
            }
        }

        /* ---------- 2026-09-11：左上「返回」键（播放态一次回浏览态，与 BACK 手势同义）；
            侧边栏打开时让位隐藏（先点空白关侧边栏，再点返回） ---------- */
        androidx.compose.animation.AnimatedVisibility(
            visible = controlsVisible && !sidebarVisible,
            enter = fadeIn(animationSpec = tween(180)),
            exit = fadeOut(animationSpec = tween(150)),
            modifier = Modifier.align(Alignment.TopStart).statusBarsPadding(),
        ) {
            MobileBackButton(onBack = { playing = false })
        }

        OtvHint(hint)
    }

    // 需求12⑧：竖屏内联频道列表（视频下方常驻，占余下全部高度）
    if (portrait && ui.channels.isNotEmpty()) {
        sidebarContent(
            Modifier
                .fillMaxWidth()
                .weight(1f)
                .background(Color(0xF00C0C10))
                .padding(start = 26f.sx(s), end = 26f.sx(s), top = 18f.sx(s), bottom = 20f.sx(s)),
        )
    }
    } // Column
}

/**
 * 浏览态页面（2026-09-04 深夜追加改版）：一级分组顶部一行（chips 横排）+
 * 二级频道下方多行多列网格（LazyVerticalGrid 纵向滚动）；点选频道 → 直播。
 * （替代本日早前的两列布局；播放态侧边栏仍为两列不变。）
 */
@OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
@Composable
private fun TvBrowseScreen(
    nav: NavController,
    ui: IptvRepository.IptvState,
    s: Float,
    portrait: Boolean,
    groupSel: Int,
    onGroupSel: (Int) -> Unit,
    hint: String?,
    onPlay: (String, IptvRepository.Channel) -> Unit,
) {
    val groups = ui.groups
    val selGroup = groups.getOrNull(groupSel) ?: ""
    val groupChannels = remember(ui.channels, selGroup) { ui.channels.filter { it.group == selGroup } }
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
        } else {
            Column(
                Modifier
                    .fillMaxSize()
                    .padding(
                        top = if (portrait) 126f.sx(s) else 150f.sx(s),
                        start = if (portrait) 34f.sx(s) else 76f.sx(s),
                        end = if (portrait) 30f.sx(s) else 76f.sx(s),
                        bottom = if (portrait) 20.dp else 30f.sx(s),
                    ),
            ) {
                /* ----- 一级：分组 chips（需求② 2026-09-12：竖屏 FlowRow 换行全量展示，不再横滑；横屏单行） ----- */
                if (portrait) {
                    androidx.compose.foundation.layout.FlowRow(
                        horizontalArrangement = Arrangement.spacedBy(12f.sx(s)),
                        verticalArrangement = Arrangement.spacedBy(10f.sx(s)),
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        groups.forEachIndexed { i, g ->
                            val active = i == groupSel
                            Box(
                                contentAlignment = Alignment.Center,
                                modifier = Modifier
                                    .clip(RoundedCornerShape(28f.sx(s)))
                                    .tapCard { onGroupSel(i) }
                                    .background(
                                        if (active) OtvColors.White else OtvColors.ChipBg,
                                        RoundedCornerShape(28f.sx(s)),
                                    )
                                    .padding(horizontal = 26f.sx(s))
                                    .height(56f.sx(s)),
                            ) {
                                Text(
                                    g,
                                    color = if (active) OtvColors.Bg else OtvColors.White.copy(alpha = 0.6f),
                                    fontSize = 24f.sxs(s),
                                    fontWeight = FontWeight.Medium,
                                    maxLines = 1,
                                )
                            }
                        }
                    }
                } else LazyRow(
                    state = rememberLazyListState(),
                    horizontalArrangement = Arrangement.spacedBy(16f.sx(s)),
                    contentPadding = PaddingValues(end = 40f.sx(s)),
                ) {
                    items(groups.size, key = { i -> "bg-$i" }) { i ->
                        val active = i == groupSel
                        Box(
                            contentAlignment = Alignment.Center,
                            modifier = Modifier
                                .clip(RoundedCornerShape(32f.sx(s)))
                                .tapCard { onGroupSel(i) }
                                .background(
                                    if (active) OtvColors.White else OtvColors.ChipBg,
                                    RoundedCornerShape(32f.sx(s)),
                                )
                                .padding(horizontal = 34f.sx(s))
                                .height(64f.sx(s)),
                        ) {
                            Text(
                                groups[i],
                                color = if (active) OtvColors.Bg else OtvColors.White.copy(alpha = 0.6f),
                                fontSize = 29f.sxs(s),
                                fontWeight = FontWeight.Medium,
                                maxLines = 1,
                            )
                        }
                    }
                }

                Spacer(Modifier.height(if (portrait) 20f.sx(s) else 26f.sx(s)))

                /* ----- 二级：频道网格（多行多列，纵向滚动） ----- */
                LazyVerticalGrid(
                    state = gridState,
                    columns = GridCells.Fixed(if (portrait) 2 else 4),
                    horizontalArrangement = Arrangement.spacedBy(if (portrait) 18f.sx(s) else 22f.sx(s)),
                    verticalArrangement = Arrangement.spacedBy(if (portrait) 14f.sx(s) else 18f.sx(s)),
                    modifier = Modifier.weight(1f).fillMaxWidth(),
                ) {
                    items(groupChannels.size, key = { i -> "bc-" + groupChannels[i].group + "|" + groupChannels[i].name }) { i ->
                        ChannelRow(
                            s = s,
                            portrait = portrait,
                            ch = groupChannels[i],
                            playing = false,
                        ) { onPlay(groupChannels[i].group, groupChannels[i]) }
                    }
                }
            }
        }

        // 顶栏 tab（浏览态恢复，与其他主页面一致）
        // 2026-09-19：补「下移即隐藏」——频道网格滚离顶部时顶栏淡出上移、回顶淡入
        //（此前影视/足球/我的都接了下潜，电视页漏掉，用户报「下移时顶部标签栏不隐藏」）
        val chromeGone by remember(gridState) {
            derivedStateOf {
                isScrolledFromTop(
                    gridState.firstVisibleItemIndex,
                    gridState.firstVisibleItemScrollOffset,
                )
            }
        }
        com.qiubo.optimaltv.ui.components.MainTabBar(
            "tv", { key -> navigateToTab(nav, key) }, { nav.navigate("search") },
            hidden = chromeGone,
        )

        OtvHint(hint)
    }
}

/**
 * 两级菜单一级分组 chip（浏览页与播放态侧边栏共用，需求⑥）：
 * 整宽胶囊；active = 白底黑字（浏览页，与全站 chips 语言一致）或 AccentBlue（侧边栏，
 * 视频上的选中强调）；未选 = 半透明深底。
 */
@Composable
private fun TvGroupChip(
    text: String,
    active: Boolean,
    accent: Boolean,
    s: Float,
    portrait: Boolean,
    onClick: () -> Unit,
) {
    Box(
        contentAlignment = Alignment.CenterStart,
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(20f.sx(s)))
            .tapCard(onClick)
            .background(
                when {
                    // 2026-09-05 视觉稿 v2-A：侧边栏浏览组 = 白 8% 微底（原 AccentBlue 实底取消）
                    active && accent -> Color(0x14FFFFFF)
                    active -> OtvColors.White
                    else -> Color.Transparent
                },
                RoundedCornerShape(20f.sx(s)),
            )
            .padding(horizontal = 22f.sx(s))
            // 2026-09-19 用户需求：一级分组按钮必须明显大于二级频道行（原一级 17/19 反而小于二级 20/24）
            .height(if (portrait) 60f.sx(s) else 68f.sx(s)),
    ) {
        Text(
            text,
            color = when {
                active && accent -> OtvColors.White
                active -> OtvColors.Bg
                else -> OtvColors.White60
            },
            fontSize = if (portrait) 24f.sxs(s) else 28f.sxs(s),
            fontWeight = if (active && accent) FontWeight.SemiBold else if (active) FontWeight.Bold else FontWeight.Medium,
            maxLines = 1, overflow = TextOverflow.Ellipsis,
        )
    }
}

/**
 * 侧边栏频道行：台标（Coil 管线，失败回退双色字母占位）+ 频道名；
 * 当前播放项微亮底 + 「正在播放」标记（Accent 色）。触控点按换台并收起侧边栏。
 * 竖屏（总体#2）行高与字号略缩，适配窄屏两列布局。
 */
@Composable
private fun ChannelRow(
    s: Float,
    portrait: Boolean,
    ch: IptvRepository.Channel,
    playing: Boolean,
    onClick: () -> Unit,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .height(if (portrait) 76f.sx(s) else 96f.sx(s))
            .clip(RoundedCornerShape(14f.sx(s)))
            .tapCard(onClick)
            .background(
                if (playing) AccentBlue.copy(alpha = 0.16f) else Color(0x0EFFFFFF),
                RoundedCornerShape(14f.sx(s)),
            )
            .border(
                1.dp,
                if (playing) AccentBlue.copy(alpha = 0.35f) else Color(0x0DFFFFFF),
                RoundedCornerShape(14f.sx(s)),
            )
            .padding(horizontal = 14f.sx(s)),
    ) {
        Box(
            Modifier
                .size(if (portrait) 46f.sx(s) else 58f.sx(s))
                .clip(RoundedCornerShape(10f.sx(s)))
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
        Spacer(Modifier.width(14f.sx(s)))
        Text(
            ch.name,
            color = if (playing) OtvColors.White else OtvColors.White75,
            fontSize = if (portrait) 18f.sxs(s) else 21f.sxs(s),
            fontWeight = if (playing) FontWeight.SemiBold else FontWeight.Normal,
            maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f, fill = false),
        )
        if (playing) {
            Spacer(Modifier.weight(1f))
            Text("正在播放", color = AccentBlue, fontSize = 15f.sxs(s), fontWeight = FontWeight.SemiBold)
        }
    }
}

/** 台标占位：频道名首二字 + 双色渐变底（PosterPlaceholder 同风格种子法） */
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
            fontSize = 22f.sxs(s),
            fontWeight = FontWeight.Bold,
        )
    }
}

/** 标准 DLNA 投屏图标（Cast 风格：双波纹 + 实心屏角）——投屏胶囊共用 */
@Composable
private fun CastGlyph(size: androidx.compose.ui.unit.Dp, color: Color) {
    Canvas(modifier = Modifier.size(size)) {
        val s = this.size.width
        drawArc(
            color = color,
            startAngle = 200f, sweepAngle = 100f, useCenter = false,
            topLeft = Offset(s * 0.08f, s * 0.30f),
            size = Size(s * 0.84f, s * 0.84f),
            style = Stroke(width = s * 0.09f, cap = StrokeCap.Round),
        )
        drawArc(
            color = color,
            startAngle = 200f, sweepAngle = 100f, useCenter = false,
            topLeft = Offset(s * 0.24f, s * 0.46f),
            size = Size(s * 0.52f, s * 0.52f),
            style = Stroke(width = s * 0.09f, cap = StrokeCap.Round),
        )
        drawRoundRect(
            color = color,
            topLeft = Offset(s * 0.42f, s * 0.66f),
            size = Size(s * 0.18f, s * 0.24f),
            cornerRadius = CornerRadius(s * 0.05f),
        )
    }
}

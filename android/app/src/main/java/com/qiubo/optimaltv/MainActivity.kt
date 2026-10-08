package com.qiubo.optimaltv

import android.annotation.SuppressLint
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import kotlinx.coroutines.launch
import com.qiubo.optimaltv.ui.all.AllScreen
import com.qiubo.optimaltv.ui.detail.DetailScreen
import com.qiubo.optimaltv.ui.favorites.FavoritesScreen
import com.qiubo.optimaltv.ui.home.VodScreen
import com.qiubo.optimaltv.ui.live.LiveScreen
import com.qiubo.optimaltv.ui.player.PlayerScreen
import com.qiubo.optimaltv.ui.search.SearchScreen
import com.qiubo.optimaltv.ui.theme.OptimalTvTheme

/** 调试直达播放页通道（v1.8 2026-08-30）：debug 构建 DebugNavReceiver 广播写入 pending，
 *  AppRoot 轮询消费——绕过模拟器丢键/焦点漂移，测试直播链路用。
 *  adb shell am broadcast -a com.qiubo.optimaltv.DEBUG_NAV --es vodId "live:45653551" */
object DebugNavBus {
    @Volatile var pendingVodId: String? = null
    @Volatile var pendingEp: Int = 0
    /** 直播引擎强制（仅测试注入：am start --es otvEngine vlc） */
    @Volatile var pendingEngine: String? = null
    /** 启动落点覆盖（仅 debug：am start --es otvDebugRoute paywall|activate|favorites）；release 无写入方 */
    @Volatile var pendingRoute: String? = null
}

/** v1.17 品牌启动屏（用户需求：内容加载好后再进入 app）：黑底 + My TV 字标 + 细转圈 */
@Composable
private fun StartupSplash() {
    val status by AppStartup.status.collectAsStateWithLifecycle()
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Image(
                painter = painterResource(com.qiubo.optimaltv.R.drawable.my_tv_logo),
                contentDescription = "My TV",
                modifier = Modifier.size(width = 220.dp, height = 220.dp),
            )
            Spacer(Modifier.height(48.dp))
            com.qiubo.optimaltv.ui.components.AppleLoading()
            // 全量预热可能十几秒到数十秒：给一行进度文案，避免看起来像卡死
            Spacer(Modifier.height(24.dp))
            Text(status, color = Color(0xFF9A9AA2), fontSize = 14.sp)
        }
    }
}

/** v1.18 强制更新阻断屏：黑底 + 字标 + 版本/阶段 + 细转圈，下载完成后自动重启生效 */
@Composable
private fun ForceUpdateScreen(state: com.qiubo.optimaltv.hotupdate.ForceState) {
    val b = state as? com.qiubo.optimaltv.hotupdate.ForceState.Blocking ?: return
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Image(
                painter = painterResource(com.qiubo.optimaltv.R.drawable.my_tv_logo),
                contentDescription = "My TV",
                modifier = Modifier.size(width = 220.dp, height = 220.dp),
            )
            Spacer(Modifier.height(40.dp))
            Text("强制更新 v" + b.version, color = Color.White, fontSize = 20.sp, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(12.dp))
            Text(b.phase, color = Color(0xFF9A9AA2), fontSize = 14.sp)
            Spacer(Modifier.height(28.dp))
            com.qiubo.optimaltv.ui.components.AppleLoading()
        }
    }
}

/** 单 Activity（方案 §3.1 UI 层）：Compose NavHost 路由（Tab 对齐原版：足球/影视/我的/搜索） */
class MainActivity : ComponentActivity() {

    // v1.13 集中式导航：四方向键在 Activity 层直接交 OtvNav 引擎，处理不了也消费——
    // Compose 焦点系统从此收不到方向键，其兜底几何搜索（乱跳根因）物理性消失。
    // v1.21 焦点乱跑根因修复（2026-09-06 仪器测试实证）：ACTION_UP 也必须一并消费——
    // 只拦 ACTION_DOWN 时，方向键的 UP 半个事件漏进 Compose，其自带几何搜索不认识
    // floating/navGroup/sameRow 规则，实测造成 UP 斜跳悬浮行、navGroup 组内 DOWN
    // 逃逸到组外卡（GAINED 日志铁证）。四方向键的 UP 事件本应用无任何消费方。
    // ComponentActivity 将该可覆写入口标记为 library-group restricted；应用必须在窗口
    // 分发层拦截完整 DPAD down/up 事件，避免 Compose 的第二套几何导航再次接管。
    @SuppressLint("RestrictedApi")
    override fun dispatchKeyEvent(event: android.view.KeyEvent): Boolean {
        if (event.action == android.view.KeyEvent.ACTION_DOWN) {
            val dir = com.qiubo.optimaltv.ui.components.navDirOf(event.keyCode)
            if (dir != null) {
                return com.qiubo.optimaltv.ui.components.OtvNav.dispatch(dir, event.repeatCount)
            }
        } else if (event.action == android.view.KeyEvent.ACTION_UP) {
            if (com.qiubo.optimaltv.ui.components.navDirOf(event.keyCode) != null) {
                return true
            }
        }
        return super.dispatchKeyEvent(event)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        com.qiubo.optimaltv.OtvLog.i("activity onCreate")
        consumeDebugNav(intent)
        syncViewportMetrics()
        // TV 沉浸式：设计稿按整屏 1920×1080 布局，状态栏不隐藏会把底部内容挤出屏
        window.decorView.systemUiVisibility = (
            android.view.View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or android.view.View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or android.view.View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                or android.view.View.SYSTEM_UI_FLAG_FULLSCREEN
                or android.view.View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or android.view.View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            )
        setContent {
            OptimalTvTheme {
                // v1.17/v1.18 启动门闸：四个标签内容源 + 影视各分类 tab 页数据与封面
                // 预热完成（或 90s 兜底超时）前渲染品牌启动屏
                val ready by AppStartup.ready.collectAsStateWithLifecycle()
                // v1.18 公测版强制更新门闸：阻断态全屏遮罩；检查未 settled 前不闪主界面
                val huForce by com.qiubo.optimaltv.hotupdate.HotUpdateManager.force.collectAsStateWithLifecycle()
                val huSettled by com.qiubo.optimaltv.hotupdate.HotUpdateManager.settled.collectAsStateWithLifecycle()
                when {
                    huForce != null -> ForceUpdateScreen(huForce!!)
                    !com.qiubo.optimaltv.BuildConfig.HOTUPDATE_ENABLED || huSettled -> if (ready) AppRoot() else StartupSplash()
                    else -> StartupSplash()
                }
            }
        }
    }

    override fun onConfigurationChanged(newConfig: android.content.res.Configuration) {
        super.onConfigurationChanged(newConfig)
        syncViewportMetrics()
    }

    /** 窗口尺寸注入导航引擎（ROTATION 感知；Resources.getSystem 在横屏下会给自然方向尺寸） */
    private fun syncViewportMetrics() {
        runCatching {
            com.qiubo.optimaltv.ui.components.OtvNav.viewportHeightPx =
                windowManager.currentWindowMetrics.bounds.height().toFloat()
        }
    }

    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        consumeDebugNav(intent)
    }

    /** 调试直达播放页：am start --es otvDebugVodId "live:<id>"（Android 15 实测
     *  shell 广播不投递 manifest receiver，改走 activity intent extra；仅测试注入用）。
     *  otvDebugQuery：注入搜索页查询词（模拟器注键丢键，搜索页测试用） */
    private fun consumeDebugNav(intent: android.content.Intent?) {
        intent?.getStringExtra("otvDebugVodId")?.takeIf { it.isNotBlank() }?.let {
            DebugNavBus.pendingVodId = it
            DebugNavBus.pendingEp = intent.getIntExtra("ep", 0)
            DebugNavBus.pendingEngine = intent.getStringExtra("otvEngine")?.takeIf { e -> e.isNotBlank() }
            com.qiubo.optimaltv.OtvLog.i("debug-nav（intent extra）vodId=$it engine=${DebugNavBus.pendingEngine ?: "默认"}")
        }
        intent?.getStringExtra("otvDebugQuery")?.takeIf { it.isNotBlank() }?.let {
            com.qiubo.optimaltv.ui.search.SearchDebugBus.pendingQuery = it
            com.qiubo.optimaltv.OtvLog.i("debug-nav（intent extra）query=$it")
        }
        // 调试直达二级页（仅 debug；release 无写入方）：
        // adb shell am start -n <pkg>/com.qiubo.optimaltv.MainActivity --es otvDebugRoute favorites
        if (com.qiubo.optimaltv.BuildConfig.DEBUG) {
            intent?.getStringExtra("otvDebugRoute")?.takeIf { it.isNotBlank() }?.let {
                DebugNavBus.pendingRoute = it
                com.qiubo.optimaltv.OtvLog.i("debug-nav（intent extra）route=$it")
            }
        }
        // v1.17 调试注入激活码（模拟器键盘注键丢键，测试用；码本身仍走完整验证链路：
        // 云端查表+设备绑定+本地 ECDSA 验签，无任何安全豁免；公测版 debug 构建限定）
        if (com.qiubo.optimaltv.BuildConfig.LICENSE_ENABLED && BuildConfig.DEBUG) {
            intent?.getStringExtra("otvDebugLicense")?.takeIf { it.isNotBlank() }?.let { code ->
                com.qiubo.optimaltv.OtvLog.i("debug-nav（intent extra）license=${code.take(4)}***")
                Graph.appScope.launch {
                    val r = com.qiubo.optimaltv.license.LicenseManager.activate(code)
                    com.qiubo.optimaltv.OtvLog.i("debug license activate → $r")
                }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        // 冷启动时 Android 可能因后台启动限制拒绝一次 startService；前台恢复后重试，
        // 避免内嵌后端未监听 8090 导致足球列表/直播一起不可用。
        BackendService.ensure(this)
        com.qiubo.optimaltv.lifecycle.AppLifecycleManager.onForeground()
        com.qiubo.optimaltv.OtvLog.i("activity ON_RESUME（窗口可见）")
        // v1.20 热更自动重启：后台完成下载的更新在回前台时立即生效
        com.qiubo.optimaltv.hotupdate.HotUpdateManager.onForegroundChanged(true)
        // 从“允许安装未知应用”系统设置返回时，继续 APK 下载并调起安装器
        com.qiubo.optimaltv.apkupdate.ApkUpdateManager.onForegroundChanged(true)
        com.qiubo.optimaltv.announcement.AnnouncementManager.onForegroundChanged(true)
        com.qiubo.optimaltv.license.LicenseManager.requestReverify()
    }

    override fun onPause() {
        com.qiubo.optimaltv.lifecycle.AppLifecycleManager.onBackground()
        super.onPause()
        com.qiubo.optimaltv.OtvLog.i("activity ON_PAUSE")
        com.qiubo.optimaltv.announcement.AnnouncementManager.onForegroundChanged(false)
        com.qiubo.optimaltv.hotupdate.HotUpdateManager.onForegroundChanged(false)
    }
    override fun onStop() {
        com.qiubo.optimaltv.apkupdate.ApkUpdateManager.onForegroundChanged(false)
        super.onStop()
        com.qiubo.optimaltv.OtvLog.w("activity ON_STOP（黑屏若发生在此后，见播放器/焦点日志上下文）")
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        com.qiubo.optimaltv.OtvLog.i("windowFocusChanged hasFocus=$hasFocus")
    }
}

@Composable
private fun AppRoot() {
    val nav = rememberNavController()

    // 调试直达播放页：debug 广播 → pending 消费（详见 DebugNavBus；release 无写入方）
    LaunchedEffect(Unit) {
        while (true) {
            DebugNavBus.pendingVodId?.let { id ->
                DebugNavBus.pendingVodId = null
                val ep = DebugNavBus.pendingEp
                com.qiubo.optimaltv.OtvLog.i("debug-nav → player vodId=$id ep=$ep")
                runCatching {
                    // v1.13 防护：player→player 直切（连续直达不同频道）会让旧 VM 的引擎
                    // 释放与新 destination 组合互锁（实测组合冻结、route 日志照打）——
                    // 先退回非 player 页再进新播放页，与真实遥控路径（经列表页中转）一致
                    if (nav.currentDestination?.route?.startsWith("player/") == true) {
                        nav.popBackStack()
                    }
                    nav.navigate("player/" + java.net.URLEncoder.encode(id, "UTF-8") + "/" + ep)
                }
            }
            kotlinx.coroutines.delay(200)
        }
    }

    // 需求 足球#3：路由变化落日志（黑屏发生在哪一页）
    androidx.compose.runtime.DisposableEffect(nav) {
        val listener = androidx.navigation.NavController.OnDestinationChangedListener { _, dest, _ ->
            com.qiubo.optimaltv.OtvLog.i("route → ${dest.route}")
        }
        nav.addOnDestinationChangedListener(listener)
        onDispose { nav.removeOnDestinationChangedListener(listener) }
    }

    // 目录刷新入口：冷启动拉取全部源；返回首页不重复刷
    LaunchedEffect(Unit) {
        Graph.repo.refresh()
    }

    // 启动门禁（2026-09-18）：开启卡密后，未激活 / 已过期 / 已封禁 → 直接落在激活页，
    // 不给进主界面。判定与 LicensePlayerGate 同款（isPremium：24h 离线宽限 + 未过期）；
    // 激活成功后 ActivateScreen 自行跳足球页（见该页 gate 分支）。
    val licenseOn = com.qiubo.optimaltv.BuildConfig.LICENSE_ENABLED
    val licState: com.qiubo.optimaltv.license.LicenseState? =
        if (licenseOn) com.qiubo.optimaltv.license.LicenseManager.state.collectAsStateWithLifecycle().value else null
    if (licenseOn && licState == null) {
        // 授权状态尚未落地（启动门闸后才读，瞬时）→ 先不建 NavHost，
        // 免得把已激活用户误判成未激活
        Box(Modifier.fillMaxSize().background(Color.Black))
        return
    }
    // 调试直达页（2026-09-19）：debug 构建下可用 adb 直接落在指定二级页，
    // 便于对会员页/激活页/我的页做版式截图与回归（release 无写入方，恒为 null）。
    // adb shell am start -n <pkg>/com.qiubo.optimaltv.MainActivity --es otvDebugRoute favorites
    val debugRoute = if (com.qiubo.optimaltv.BuildConfig.DEBUG) {
        DebugNavBus.pendingRoute
    } else null
    val startDestination = debugRoute
        ?: if (licenseOn && !com.qiubo.optimaltv.license.LicenseManager.isPremium()) "activate" else "live"

    // v1.20 全局浮层：运营公告（顶部半透明悬浮窗）+ 非强更提案弹窗 + 下载进度层
    Box(Modifier.fillMaxSize()) {
        NavHost(navController = nav, startDestination = startDestination) {
            composable("live") { LiveScreen(nav) }

        composable("vod") { VodScreen(nav) }

        // 电视直播页（v1.13：公开 IPTV 源原生直连，紧随影视之后）
        // v1.17 公测版：电视页为内嵌播放（不走 player 路由），门控须在路由层拦截——
        // 未激活渲染会员页不进电视页；内测版直接透传
        composable("tv") {
            com.qiubo.optimaltv.ui.paywall.LicensePlayerGate(nav) {
                com.qiubo.optimaltv.ui.tv.TvScreen(nav)
            }
        }

        // 全部影视页（原版 view-all：片库「全部 ›」跳转，筛选+分页海报墙）
        composable("all") { AllScreen(nav) }

        composable("favorites") { FavoritesScreen(nav) }

        composable(
            route = "detail/{vodId}",
            arguments = listOf(navArgument("vodId") { type = NavType.StringType }),
        ) { entry ->
            DetailScreen(nav, java.net.URLDecoder.decode(entry.arguments?.getString("vodId").orEmpty(), "UTF-8"))
        }

        composable(
            // v1.19 跨线路续播：resumeMs 可选参数——详情页已扫全部线路取到最近观看位置，
            // 直接带给播放器（旧版播放器只查当前线路 key，换过线的影片续播从 0 重播）
            route = "player/{vodId}/{ep}?resumeMs={resumeMs}",
            arguments = listOf(
                navArgument("vodId") { type = NavType.StringType },
                navArgument("ep") { type = NavType.IntType; defaultValue = 0 },
                navArgument("resumeMs") { type = NavType.LongType; defaultValue = 0L },
            ),
            // v1.21 光标乱跑根因修复（2026-09-06 黑盒复现）：player 路由退出 crossfade
            // 转场会卡在未完成态——popBackStack 后海报墙已可见，但 PlayerScreen 组合
            // 以 alpha≈0 残留：视频后台续播、全屏 rootFocus sink 仍持焦 → 四向全 blocked
            // 光标死锁，OK 还会戳到隐形播放器控制层。TV 播放器改 None 转场（进出/pop
            // 均瞬时）——pop 即刻销毁组合，navSink 注销、dpadFocusable 触发补焦回列表。
            enterTransition = { androidx.compose.animation.EnterTransition.None },
            exitTransition = { androidx.compose.animation.ExitTransition.None },
            popEnterTransition = { androidx.compose.animation.EnterTransition.None },
            popExitTransition = { androidx.compose.animation.ExitTransition.None },
        ) { entry ->
            // v1.17 全量门控收口点（方案 §4.5）：公测版统一播放入口一处拦截——
            // 未激活渲染会员页不进播放器；内测版直接透传零差异
            com.qiubo.optimaltv.ui.paywall.LicensePlayerGate(nav) {
                PlayerScreen(
                    nav,
                    java.net.URLDecoder.decode(entry.arguments?.getString("vodId").orEmpty(), "UTF-8"),
                    entry.arguments?.getInt("ep") ?: 0,
                    entry.arguments?.getLong("resumeMs") ?: 0L,
                )
            }
        }

        composable("search") { SearchScreen(nav) }

        // v1.17 公测版：会员页（我的页入口/角标跳转）与激活输码页
        composable("paywall") { com.qiubo.optimaltv.ui.paywall.PaywallScreen(nav) }
        composable("activate") { com.qiubo.optimaltv.ui.paywall.ActivateScreen(nav) }
        }
        // 全局浮层（最顶层，非模态——公告悬浮窗不拦内容导航）
        // 后端热更已改为后台静默自动更新，不再有热更弹窗/进度层
        com.qiubo.optimaltv.ui.components.AnnouncementBanner()
        com.qiubo.optimaltv.ui.components.ApkUpdateUi()
    }
}

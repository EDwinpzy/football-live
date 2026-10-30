package com.qiubo.optimaltv

import android.annotation.SuppressLint
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
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
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import kotlinx.coroutines.launch
import com.qiubo.optimaltv.ui.live.LiveScreen
import com.qiubo.optimaltv.ui.player.PlayerScreen
import com.qiubo.optimaltv.ui.theme.QiuJiLiveTheme

/** 调试直达播放页通道（v1.8 2026-08-30）：debug 构建 DebugNavReceiver 广播写入 pending，
 *  AppRoot 轮询消费——绕过模拟器丢键/焦点漂移，测试直播链路用。
 *  adb shell am broadcast -a com.qiubo.optimaltv.DEBUG_NAV --es matchId "45653551" */
object DebugNavBus {
    @Volatile var pendingMatchId: String? = null
    /** 直播引擎强制（仅测试注入：am start --es otvEngine vlc） */
    @Volatile var pendingEngine: String? = null
}

/** 球迹直播启动屏：黑底、品牌图标与加载提示。 */
@Composable
private fun StartupSplash() {
    val status by AppStartup.status.collectAsStateWithLifecycle()
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text("球迹直播", color = Color.White, fontSize = 38.sp)
            Spacer(Modifier.height(40.dp))
            com.qiubo.optimaltv.ui.components.AppleLoading()
            // 全量预热可能十几秒到数十秒：给一行进度文案，避免看起来像卡死
            Spacer(Modifier.height(24.dp))
            Text(status, color = Color(0xFF9A9AA2), fontSize = 14.sp)
        }
    }
}

/** 单 Activity：唯一主页面为足球赛程，另含足球播放器。 */
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
                return com.qiubo.optimaltv.ui.components.OtvNav.dispatch(
                    dir, event.repeatCount, (event.eventTime - event.downTime).coerceAtLeast(0),
                )
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
        // TV 沉浸式：球迹直播使用完整 16:9 画布，系统栏不隐藏会把内容挤出屏幕
        window.decorView.systemUiVisibility = (
            android.view.View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or android.view.View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or android.view.View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                or android.view.View.SYSTEM_UI_FLAG_FULLSCREEN
                or android.view.View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or android.view.View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            )
        setContent {
            QiuJiLiveTheme {
                // 足球赛程页面就绪前渲染品牌启动屏。
                val ready by AppStartup.ready.collectAsStateWithLifecycle()
                // 热更新在后台静默处理；无论服务端 force 标记如何，都不阻断使用。
                val huSettled by com.qiubo.optimaltv.hotupdate.HotUpdateManager.settled.collectAsStateWithLifecycle()
                when {
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

    /** 调试直达足球播放页：am start --es otvDebugMatchId "<id>"；仅测试注入用。 */
    private fun consumeDebugNav(intent: android.content.Intent?) {
        intent?.getStringExtra("otvDebugMatchId")?.takeIf { it.isNotBlank() }?.let {
            DebugNavBus.pendingMatchId = it.removePrefix("live:")
            DebugNavBus.pendingEngine = intent.getStringExtra("otvEngine")?.takeIf { e -> e.isNotBlank() }
            com.qiubo.optimaltv.OtvLog.i("debug-nav（intent extra）matchId=$it engine=${DebugNavBus.pendingEngine ?: "默认"}")
        }
        // Debug direct-player entry accepts football matches only.
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
    }

    override fun onPause() {
        com.qiubo.optimaltv.lifecycle.AppLifecycleManager.onBackground()
        super.onPause()
        com.qiubo.optimaltv.OtvLog.i("activity ON_PAUSE")
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
            DebugNavBus.pendingMatchId?.let { id ->
                DebugNavBus.pendingMatchId = null
                com.qiubo.optimaltv.OtvLog.i("debug-nav → player matchId=$id")
                runCatching {
                    // v1.13 防护：player→player 直切（连续直达不同频道）会让旧 VM 的引擎
                    // 释放与新 destination 组合互锁（实测组合冻结、route 日志照打）——
                    // 先退回非 player 页再进新播放页，与真实遥控路径（经列表页中转）一致
                    if (nav.currentDestination?.route?.startsWith("player/") == true) {
                        nav.popBackStack()
                    }
                    nav.navigate("player/" + java.net.URLEncoder.encode(id, "UTF-8"))
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

    // Global update surface remains available independently of the football screen.
    Box(Modifier.fillMaxSize()) {
        NavHost(
            navController = nav,
            startDestination = "live",
            // Release the outgoing full-screen layer early; keep the incoming fade continuous.
            enterTransition = { androidx.compose.animation.fadeIn(androidx.compose.animation.core.tween(240)) },
            exitTransition = { androidx.compose.animation.fadeOut(androidx.compose.animation.core.tween(120)) },
            popEnterTransition = { androidx.compose.animation.fadeIn(androidx.compose.animation.core.tween(240)) },
            popExitTransition = { androidx.compose.animation.fadeOut(androidx.compose.animation.core.tween(120)) },
        ) {
            composable("live") { LiveScreen(nav) }

        composable(
            route = "player/{matchId}",
            arguments = listOf(
                navArgument("matchId") { type = NavType.StringType },
            ),
            // v1.21 光标乱跑根因修复（2026-09-06 黑盒复现）：player 路由退出 crossfade
            // 转场会卡在未完成态——popBackStack 后赛程列表已可见，但 PlayerScreen 组合
            // 以 alpha≈0 残留：视频后台续播、全屏 rootFocus sink 仍持焦 → 四向全 blocked
            // 光标死锁，OK 还会戳到隐形播放器控制层。TV 播放器改 None 转场（进出/pop
            // 均瞬时）——pop 即刻销毁组合，navSink 注销、dpadFocusable 触发补焦回列表。
            enterTransition = { androidx.compose.animation.EnterTransition.None },
            exitTransition = { androidx.compose.animation.ExitTransition.None },
            popEnterTransition = { androidx.compose.animation.EnterTransition.None },
            popExitTransition = { androidx.compose.animation.ExitTransition.None },
        ) { entry ->
            PlayerScreen(
                nav,
                entry.arguments?.getString("matchId").orEmpty(),
            )
        }
        }
        // Backend hot updates remain silent; APK updates may still show their update UI.
        com.qiubo.optimaltv.ui.components.ApkUpdateUi()
    }
}

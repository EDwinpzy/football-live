package com.qiubo.optimaltv

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
import androidx.core.view.WindowCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.qiubo.optimaltv.ui.live.LiveScreen
import com.qiubo.optimaltv.ui.player.PlayerScreen
import com.qiubo.optimaltv.ui.theme.ZuoLiveTheme
import kotlinx.coroutines.launch

/** 足球播放器调试入口（release 无写入方）。
 *  adb shell am start --es otvDebugMatchId "45653551" */
object DebugNavBus {
    @Volatile var pendingMatchId: String? = null
    /** 直播引擎强制（仅测试注入：am start --es otvEngine vlc） */
    @Volatile var pendingEngine: String? = null
}

/** 足球直播启动屏：品牌图标、名称与加载提示。 */
@Composable
private fun StartupSplash() {
    val status by AppStartup.status.collectAsStateWithLifecycle()
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Image(
                painter = painterResource(com.qiubo.optimaltv.R.drawable.my_tv_logo),
                contentDescription = "足球直播",
                modifier = Modifier.size(110.dp),
            )
            Text(
                "足球直播",
                color = Color.White,
                fontSize = 26.sp,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.padding(top = 18.dp),
            )
            Spacer(Modifier.height(32.dp))
            com.qiubo.optimaltv.ui.components.AppleLoading()
            // 全量预热可能十几秒到数十秒：给一行进度文案，避免看起来像卡死
            Spacer(Modifier.height(20.dp))
            Text(status, color = Color(0xFF9A9AA2), fontSize = 13.sp)
        }
    }
}

/**
 * 移动版（手机/平板触屏）单 Activity：
 * - v1.18 前端样式/布局与 TV 版 1:1 同款（足球直播横屏布局，锁横屏 +
 *   沉浸式全屏），顶部 Tab Bar 由各主 tab 屏自带（与 TV 版同构）；
 * - 无 D-pad 焦点引擎（TV 版 OtvNav 整套不参与），交互全部触控；
 * - 不显示状态栏/手势条（TV 沉浸式同款），保证 16:9 完整画布。
 */
class MainActivity : ComponentActivity() {

    @Suppress("DEPRECATION")
    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) {
            // Android 11 can clear the controller behavior when KEEP_SCREEN_ON changes.
            // Use the legacy sticky flag on API 30, matching the AndroidX workaround.
            androidx.core.view.WindowInsetsControllerCompat(window, window.decorView).apply {
                if (android.os.Build.VERSION.SDK_INT == android.os.Build.VERSION_CODES.R) {
                    val decor = window.decorView
                    decor.systemUiVisibility = (decor.systemUiVisibility and
                        android.view.View.SYSTEM_UI_FLAG_IMMERSIVE.inv()) or
                        android.view.View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                } else {
                    systemBarsBehavior = androidx.core.view.WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
                }
                hide(androidx.core.view.WindowInsetsCompat.Type.systemBars())
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        com.qiubo.optimaltv.OtvLog.i("activity onCreate（mobile）")
        consumeDebugNav(intent)
        // v1.18 沉浸式全屏（与 TV 版同款）：足球直播使用完整 16:9 画布，系统栏不隐藏会把内容挤出屏幕
        androidx.core.view.WindowCompat.setDecorFitsSystemWindows(window, false)
        setContent {
            ZuoLiveTheme {
                // 足球比赛列表准备后显示直播首页。
                val ready by AppStartup.ready.collectAsStateWithLifecycle()
                // 热更只做后台静默下载；远端强更标记不会覆盖首页或阻断进入。
                // 首次元数据检查完成后再显示主界面，下载过程不参与启动门闸。
                val huSettled by com.qiubo.optimaltv.hotupdate.HotUpdateManager.settled.collectAsStateWithLifecycle()
                when {
                    !com.qiubo.optimaltv.BuildConfig.HOTUPDATE_ENABLED || huSettled -> if (ready) AppRoot() else StartupSplash()
                    else -> StartupSplash()
                }
            }
        }
    }

    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        consumeDebugNav(intent)
    }

    /** 调试直达足球播放页（仅测试注入用） */
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
        com.qiubo.optimaltv.OtvLog.i("activity ON_RESUME（mobile）")
        // v1.20 热更自动重启：后台完成下载的更新在回前台时立即生效
        com.qiubo.optimaltv.hotupdate.HotUpdateManager.onForegroundChanged(true)
        // 从“允许安装未知应用”系统设置返回时，继续 APK 下载并调起安装器
        com.qiubo.optimaltv.apkupdate.ApkUpdateManager.onForegroundChanged(true)
    }

    override fun onPause() {
        com.qiubo.optimaltv.lifecycle.AppLifecycleManager.onBackground()
        super.onPause()
        com.qiubo.optimaltv.OtvLog.i("activity ON_PAUSE（mobile）")
        com.qiubo.optimaltv.hotupdate.HotUpdateManager.onForegroundChanged(false)
    }
}

/** v1.18：底部导航已移除——顶部 Tab Bar 由各主 tab 屏自带（与 TV 版同构） */
@Composable
private fun AppRoot() {
    val nav = rememberNavController()

    // 调试直达播放页：pending 消费（与 TV 版同款）
    LaunchedEffect(Unit) {
        while (true) {
            DebugNavBus.pendingMatchId?.let { id ->
                DebugNavBus.pendingMatchId = null
                com.qiubo.optimaltv.OtvLog.i("debug-nav → player matchId=$id")
                runCatching {
                    if (nav.currentDestination?.route?.startsWith("player/") == true) {
                        nav.popBackStack()
                    }
                    nav.navigate("player/" + java.net.URLEncoder.encode(id, "UTF-8"))
                }
            }
            kotlinx.coroutines.delay(200)
        }
    }

    // 路由变化落日志（排查黑屏/去向）
    androidx.compose.runtime.DisposableEffect(nav) {
        val listener = androidx.navigation.NavController.OnDestinationChangedListener { _, dest, _ ->
            com.qiubo.optimaltv.OtvLog.i("route → ${dest.route}")
        }
        nav.addOnDestinationChangedListener(listener)
        onDispose { nav.removeOnDestinationChangedListener(listener) }
    }

    // APK update UI remains available independently of the football screen.
    Box(Modifier.fillMaxSize()) {
        NavHost(
            navController = nav,
            startDestination = "live",
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
                ) { entry ->
                    PlayerScreen(
                        nav,
                        entry.arguments?.getString("matchId").orEmpty(),
                    )
                }
        }
        // 全局浮层（最顶层，非模态）。
        // 后端热更已改为后台静默自动更新，不再有热更弹窗/进度层
        com.qiubo.optimaltv.ui.components.ApkUpdateUi()
    }
}

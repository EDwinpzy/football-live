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
import androidx.compose.ui.unit.sp
import androidx.core.view.WindowCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.qiubo.optimaltv.ui.all.AllScreen
import com.qiubo.optimaltv.ui.detail.DetailScreen
import com.qiubo.optimaltv.ui.favorites.FavoritesScreen
import com.qiubo.optimaltv.ui.home.VodScreen
import com.qiubo.optimaltv.ui.live.LiveScreen
import com.qiubo.optimaltv.ui.player.PlayerScreen
import com.qiubo.optimaltv.ui.search.SearchScreen
import com.qiubo.optimaltv.ui.theme.OptimalTvTheme
import kotlinx.coroutines.launch

/** 调试直达播放页通道（与 TV 版同款，MuMu 测试注入用；release 无写入方）。
 *  adb shell am start --es otvDebugVodId "live:45653551" */
object DebugNavBus {
    @Volatile var pendingVodId: String? = null
    @Volatile var pendingEp: Int = 0
    /** 直播引擎强制（仅测试注入：am start --es otvEngine vlc） */
    @Volatile var pendingEngine: String? = null
    /** 启动落点覆盖（仅 debug：am start --es otvDebugRoute paywall|activate）；release 无写入方 */
    @Volatile var pendingRoute: String? = null
}

/** v1.18 强制更新阻断屏（与 TV 版同款）：黑底 + 字标 + 版本/阶段 + 细转圈 */
@Composable
private fun ForceUpdateScreen(state: com.qiubo.optimaltv.hotupdate.ForceState) {
    val b = state as? com.qiubo.optimaltv.hotupdate.ForceState.Blocking ?: return
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Image(
                painter = painterResource(com.qiubo.optimaltv.R.drawable.my_tv_logo),
                contentDescription = "My TV",
                modifier = Modifier.size(110.dp),
            )
            Spacer(Modifier.height(36.dp))
            Text("强制更新 v" + b.version, color = Color.White, fontSize = 20.sp, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(12.dp))
            Text(b.phase, color = Color(0xFF9A9AA2), fontSize = 14.sp)
            Spacer(Modifier.height(28.dp))
            com.qiubo.optimaltv.ui.components.AppleLoading()
        }
    }
}

/** v1.17 品牌启动屏（内容加载好后再进入 app）：黑底 + My TV 标 + 细转圈 */
@Composable
private fun StartupSplash() {
    val status by AppStartup.status.collectAsStateWithLifecycle()
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Image(
                painter = painterResource(com.qiubo.optimaltv.R.drawable.my_tv_logo),
                contentDescription = "My TV",
                modifier = Modifier.size(110.dp),
            )
            Text(
                "My TV",
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
 * - v1.18 前端样式/布局与 TV 版 1:1 同款（My TV 1920 设计稿横屏布局，锁横屏 +
 *   沉浸式全屏），顶部 Tab Bar 由各主 tab 屏自带（与 TV 版同构）；
 * - 无 D-pad 焦点引擎（TV 版 OtvNav 整套不参与），交互全部触控；
 * - 不显示状态栏/手势条（TV 沉浸式同款），保证 16:9 完整画布。
 */
class MainActivity : ComponentActivity() {

    /** 2026-09-05 用户需求：遥控器/键盘「确定」键与触屏同权——电视页播放态据此弹出
     *  选台竖条与投屏胶囊（盒子/投影仪用户无触屏，此前无法唤出选台侧边栏） */
    override fun onKeyDown(keyCode: Int, event: android.view.KeyEvent?): Boolean {
        when (keyCode) {
            android.view.KeyEvent.KEYCODE_ENTER,
            android.view.KeyEvent.KEYCODE_DPAD_CENTER,
            android.view.KeyEvent.KEYCODE_BUTTON_A,
            -> com.qiubo.optimaltv.ui.tv.OkKeyBus.poke()
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        com.qiubo.optimaltv.OtvLog.i("activity onCreate（mobile）")
        consumeDebugNav(intent)
        // v1.18 沉浸式全屏（与 TV 版同款）：设计稿按整屏 1920×1080 布局，系统栏不隐藏会把底部内容挤出屏
        androidx.core.view.WindowCompat.setDecorFitsSystemWindows(window, false)
        androidx.core.view.WindowInsetsControllerCompat(window, window.decorView).apply {
            hide(androidx.core.view.WindowInsetsCompat.Type.systemBars())
            systemBarsBehavior = androidx.core.view.WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        }
        // 需求⑪（2026-09-07）：公告走系统通知——Android 13+ 需运行时申请通知权限
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 4701)
        }
        setContent {
            OptimalTvTheme {
                // v1.17 启动门闸：四个标签内容源预热完成（或 45s 兜底）前渲染品牌启动屏
                val ready by AppStartup.ready.collectAsStateWithLifecycle()
                // v1.19 移植热更新（与 TV 版同款）：强制更新阻断态全屏遮罩；
                // check 未 settled 前不闪主界面（HOTUPDATE_ENABLED=false 时零行为）
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

    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        consumeDebugNav(intent)
    }

    /** 调试直达播放页/注入搜索词（与 TV 版同款，仅测试注入用） */
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
        // adb shell am start -n <pkg>/com.qiubo.optimaltv.MainActivity --es otvDebugRoute paywall
        if (com.qiubo.optimaltv.BuildConfig.DEBUG) {
            intent?.getStringExtra("otvDebugRoute")?.takeIf { it.isNotBlank() }?.let {
                DebugNavBus.pendingRoute = it
                com.qiubo.optimaltv.OtvLog.i("debug-nav（intent extra）route=$it")
            }
        }
        // v1.17 调试注入激活码（模拟器注键丢键，测试用；码本身仍走完整验证链路：
        // 云端查表+设备绑定+本地 ECDSA 验签，无任何安全豁免；公测版 debug 构建限定）
        if (com.qiubo.optimaltv.BuildConfig.LICENSE_ENABLED && com.qiubo.optimaltv.BuildConfig.DEBUG) {
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
        com.qiubo.optimaltv.OtvLog.i("activity ON_RESUME（mobile）")
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
        com.qiubo.optimaltv.OtvLog.i("activity ON_PAUSE（mobile）")
        com.qiubo.optimaltv.announcement.AnnouncementManager.onForegroundChanged(false)
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
            DebugNavBus.pendingVodId?.let { id ->
                DebugNavBus.pendingVodId = null
                val ep = DebugNavBus.pendingEp
                com.qiubo.optimaltv.OtvLog.i("debug-nav → player vodId=$id ep=$ep")
                runCatching {
                    if (nav.currentDestination?.route?.startsWith("player/") == true) {
                        nav.popBackStack()
                    }
                    nav.navigate("player/" + java.net.URLEncoder.encode(id, "UTF-8") + "/" + ep)
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

    // 冷启动拉取全部源（AppStartup 已预热，这里兜底重试）
    LaunchedEffect(Unit) {
        Graph.repo.refresh()
    }

    // 启动门禁（2026-09-18）：开启卡密后，未激活 / 已过期 / 已封禁 → 直接落在激活页，
    // 不给进主界面。判定与 LicensePlayerGate 同款（isPremium：24h 离线宽限 + 未过期）；
    // 激活成功后 ActivateScreen 自行跳足球页（见该页 resultOk 分支）。
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
    // 便于对会员页/激活页做版式截图与回归（release 无写入方，恒为 null）。
    // adb shell am start -n <pkg>/com.qiubo.optimaltv.MainActivity --es otvDebugRoute paywall
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

                // 电视页为内嵌播放（不走 player 路由），门控须在路由层拦截（与 TV 版同构）；
                // 未激活渲染会员页不进电视页；内测版直接透传
                composable("tv") {
                    com.qiubo.optimaltv.ui.paywall.LicensePlayerGate(nav) {
                        com.qiubo.optimaltv.ui.tv.TvScreen(nav)
                    }
                }

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
        // 全局浮层（最顶层，非模态）；需求⑪：公告已改系统通知，AnnouncementBanner 移除
        com.qiubo.optimaltv.ui.components.HotUpdateOfferDialog()
        com.qiubo.optimaltv.ui.components.HotUpdateProgressLayer()
        com.qiubo.optimaltv.ui.components.ApkUpdateUi()
    }
}

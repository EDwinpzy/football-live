package com.qiubo.optimaltv.focus

import android.annotation.SuppressLint
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.qiubo.optimaltv.ui.components.OtvNav
import com.qiubo.optimaltv.ui.components.navDirOf

/**
 * 焦点导航仪器测试 harness（三层测试体系第二层，2026-09-06）。
 * 放 debug 变体源集：随主 APK 打包、原生跑在主进程，androidTest 经
 * createAndroidComposeRule 直接拉起（跨 APK 跨进程启动在 MuMu 上不稳，弃）。
 *
 * dispatchKeyEvent 与 MainActivity 逐行同构（ACTION_DOWN 四方向键 → OtvNav.dispatch，
 * 处理不了也消费）——测试按键经 Compose performKeyInput → Espresso sendKeySync 注入，
 * 走与遥控器完全相同的 Activity 窗口分发链，端到端覆盖引擎。
 *
 * 场景注入：各用例把 @Composable 场景写进 Harness.scene（组合期读取的 mutableState，
 * 设置后重组生效），Activity 只负责挂载与键路由，不掺任何业务。
 */
object Harness {
    var scene by mutableStateOf<(@Composable () -> Unit)?>(null)
}

class FocusHarnessActivity : ComponentActivity() {

    @SuppressLint("RestrictedApi")
    override fun dispatchKeyEvent(event: android.view.KeyEvent): Boolean {
        if (event.action == android.view.KeyEvent.ACTION_DOWN) {
            val dir = navDirOf(event.keyCode)
            if (dir != null) {
                return OtvNav.dispatch(
                    dir,
                    event.repeatCount,
                    (event.eventTime - event.downTime).coerceAtLeast(0L),
                )
            }
        } else if (event.action == android.view.KeyEvent.ACTION_UP) {
            // 与 MainActivity 同构：方向键 UP 半事件也消费（乱跑根因修复，见其注释）
            if (navDirOf(event.keyCode) != null) return true
        }
        return super.dispatchKeyEvent(event)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        android.util.Log.i("OTV-TEST", "harness onCreate")
        window.decorView.systemUiVisibility = (
            android.view.View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or android.view.View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or android.view.View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                or android.view.View.SYSTEM_UI_FLAG_FULLSCREEN
                or android.view.View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or android.view.View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            )
        setContent {
            androidx.compose.runtime.SideEffect { android.util.Log.i("OTV-TEST", "harness composed scene=${Harness.scene != null}") }
            Harness.scene?.invoke()
        }
    }
}

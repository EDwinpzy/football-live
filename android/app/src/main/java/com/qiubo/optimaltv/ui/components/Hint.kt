package com.qiubo.optimaltv.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs

/**
 * 主 tab 已到顶时按返回：**双击退出**（用户 2026-09-19 要求：按一下只提示、连按两下才退，
 * 防误触）。窗口内第二次按才执行完整退出，第一次不改变任何生命周期状态。
 *
 * 历史口径（别搞反）：2026-09-16 曾把「2.5s 内连按两次才退出」改成「一次即退」，
 * 原因是当时旧双击实现里**第一次按下就已经把 Activity 交回桌面**、进程与内嵌后端全活着，
 * 再次进入复用那个卡死会话，于是「退出重进还是卡」。现在第一次按键纯提示（不 finish、
 * 不停后端），只有真正触发的那一次才走 ExitCoordinator：释放播放器/解码器 → 关解密桥 →
 * 停内嵌后端 → finishAffinity → 杀主进程，所以不会再复现旧毛病。
 */
@Composable
fun rememberDoubleBackExit(onHint: (String) -> Unit): () -> Unit {
    val activity = androidx.compose.ui.platform.LocalContext.current as? android.app.Activity
    var lastBackAt by remember { mutableLongStateOf(0L) }
    return {
        val now = android.os.SystemClock.elapsedRealtime()
        if (now - lastBackAt <= DOUBLE_BACK_WINDOW_MS) {
            lastBackAt = 0L
            onHint("已退出应用")
            activity?.let { com.qiubo.optimaltv.lifecycle.ExitCoordinator.exit(it) }
        } else {
            lastBackAt = now
            onHint("再按一次退出应用")
        }
    }
}

/** 双击退出的时间窗（用户连按两次判定为「要退出」） */
private const val DOUBLE_BACK_WINDOW_MS = 2500L

/**
 * 底部轻提示（需求 足球#5：只保留文字，背景半透明灰色）。
 * 替代系统 Toast（自带深色底样式不可控）；文案 2.5s 自动消失由调用方管理。
 * 提示统一贴屏幕最下方（底边距 40）。
 */
@Composable
fun BoxScope.OtvHint(text: String?, modifier: Modifier = Modifier) {
    if (text.isNullOrBlank()) return
    val s = rememberUiScale()
    Box(
        modifier
            .align(Alignment.BottomCenter)
            .padding(bottom = 40f.sx(s))
            .background(Color(0x8C808080), RoundedCornerShape(24f.sx(s)))
            .padding(horizontal = 36f.sx(s), vertical = 16f.sx(s)),
    ) {
        Text(
            text, color = Color.White, fontSize = 26f.sxs(s), fontWeight = FontWeight.Medium,
        )
    }
}

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
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 主 tab 已到顶时按返回 = 彻底退出应用（用户 2026-09-16 明确要求，与 TV 版同步）。
 *
 * 旧实现是「2.5s 内连按两次才退出」，第一次只弹提示——实测按一次返回后进程与内嵌
 * 后端都还活着（只是回了桌面），再次进入直接复用那个已经卡死的会话。现在一次返回
 * 就执行 ExitCoordinator 的完整退出。
 */
@Composable
fun rememberDoubleBackExit(onHint: (String) -> Unit): () -> Unit {
    val activity = androidx.compose.ui.platform.LocalContext.current as? android.app.Activity
    return {
        onHint("已退出应用")
        activity?.let { com.qiubo.optimaltv.lifecycle.ExitCoordinator.exit(it) }
    }
}

/** 底部轻提示（需求 足球#5：只保留文字，背景半透明灰色；v1.18 尺寸与 TV 版 1:1 同款）。
 *  需求 影视#3：全站浮窗提示统一贴屏幕最下方（底边距 40，不再浮在半空）。
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

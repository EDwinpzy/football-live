package com.qiubo.optimaltv.ui.components

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.foundation.background
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.qiubo.optimaltv.ui.theme.Brand

/**
 * 加载转圈（Apple 风格配色）：系统灰白轨道 + 白色进度弧，
 * 对齐 tvOS 加载指示（白系、无彩色），替换原 Material 蓝。
 * 播放页缓冲指示保留使用；页面级加载改用 [BrandDotsLoading]。
 */
@Composable
fun AppleLoading(modifier: Modifier = Modifier, size: Dp = 48.dp) {
    CircularProgressIndicator(
        modifier = modifier.size(size),
        color = Color.White,
        trackColor = Color.White.copy(alpha = 0.18f),
        strokeWidth = 3.5.dp,
    )
}

/**
 * 页面级加载态（SPEC v1「空态/加载态」）：居中 brand 色 3 点呼吸。
 * 三点相位错开（initialStartDelay 递进），呼吸节奏 900ms。
 */
@Composable
fun BrandDotsLoading(modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition(label = "brandDots")
    Row(modifier, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        repeat(3) { i ->
            val alpha by transition.animateFloat(
                initialValue = 0.25f,
                targetValue = 1f,
                animationSpec = infiniteRepeatable(
                    animation = tween(900, easing = LinearEasing),
                    repeatMode = RepeatMode.Reverse,
                    initialStartOffset = androidx.compose.animation.core.StartOffset(i * 300),
                ),
                label = "brandDot$i",
            )
            Box(
                Modifier
                    .size(8.dp)
                    .clip(CircleShape)
                    .background(Brand.copy(alpha = alpha), CircleShape),
            )
        }
    }
}

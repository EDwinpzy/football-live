package com.qiubo.optimaltv.ui.components

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.qiubo.optimaltv.ui.theme.OtvColors
import com.qiubo.optimaltv.ui.theme.rememberUiScale
import com.qiubo.optimaltv.ui.theme.sx
import com.qiubo.optimaltv.ui.theme.sxs

/**
 * 加载转圈（Apple 风格配色）：系统灰白轨道 + 白色进度弧，
 * 对齐 tvOS 加载指示（白系、无彩色），替换原 Material 蓝。
 * 播放页缓冲等沿用；页面级加载改用 [BreathingDots]（规范「空态/加载态」）。
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
 * 规范加载态：居中 brand 色 3 点呼吸 + caption「正在获取赛程」。
 * 三点相位错开（delay 比例），透明度 0.25↔1 呼吸。
 */
@Composable
fun BreathingDots(modifier: Modifier = Modifier, caption: String? = null) {
    val s = rememberUiScale()
    val transition = rememberInfiniteTransition(label = "breathingDots")
    val alphas = (0..2).map { phase ->
        transition.animateFloat(
            initialValue = 0.25f,
            targetValue = 1f,
            animationSpec = infiniteRepeatable(
                animation = tween(700, delayMillis = phase * 180, easing = LinearEasing),
                repeatMode = RepeatMode.Reverse,
            ),
            label = "dot$phase",
        )
    }
    Column(modifier, horizontalAlignment = Alignment.CenterHorizontally) {
        Row(horizontalArrangement = Arrangement.spacedBy(14f.sx(s))) {
            alphas.forEach { a ->
                androidx.compose.foundation.layout.Box(
                    Modifier
                        .size(12f.sx(s))
                        .background(OtvColors.Brand.copy(alpha = a.value), CircleShape),
                )
            }
        }
        if (caption != null) {
            androidx.compose.foundation.layout.Spacer(Modifier.size(18f.sx(s)))
            Text(
                caption,
                color = OtvColors.TextSecondary,
                fontSize = 15f.sxs(s),
                fontWeight = FontWeight.Medium,
            )
        }
    }
}

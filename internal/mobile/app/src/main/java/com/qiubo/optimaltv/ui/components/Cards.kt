package com.qiubo.optimaltv.ui.components

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import com.qiubo.optimaltv.data.db.HistoryEntity
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.data.model.formatTime
import com.qiubo.optimaltv.ui.theme.OtvColors

/** 渐变占位海报（图片源不可达时的兜底视觉；与 TV 版 Focus.kt 同款种子法） */
@Composable
fun PosterPlaceholder(seed: Int, modifier: Modifier = Modifier) {
    val palettes = listOf(
        listOf(Color(0xFF20304A), Color(0xFF0E1626)),
        listOf(Color(0xFF3A2545), Color(0xFF160F1D)),
        listOf(Color(0xFF1E3B33), Color(0xFF0D1A16)),
        listOf(Color(0xFF453225), Color(0xFF1B140E)),
    )
    Box(modifier.background(Brush.linearGradient(palettes[Math.floorMod(seed, palettes.size)])))
}

/** 触控卡片修饰符：点击涟漪 + 按压缩放反馈（替代 TV 版 dpadFocusable 的聚焦放大） */
fun Modifier.tapCard(onClick: () -> Unit): Modifier = composed {
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val scale = animateFloatAsState(if (pressed) 0.96f else 1f, label = "tapScale")
    this
        .clickable(interactionSource = interaction, indication = null, onClick = onClick)
        // Read animated state in the layer block so each press frame updates the transform
        // without recomposing the card content or running layout again.
        .graphicsLayer {
            scaleX = scale.value
            scaleY = scale.value
        }
}

/**
 * 竖版海报卡（v1.18 简化壳：各屏已按 TV 版视觉在本屏内绘制卡片——HomeScreen.LibCard /
 * HistoryCard、AllScreen 海报墙等；此通用组件保留给「我的/搜索」等简单列表场景，
 * 尺寸由调用方按设计稿缩放传入）。
 */

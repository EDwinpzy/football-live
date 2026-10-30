package com.qiubo.optimaltv.ui.components

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.graphicsLayer

/** Touch feedback shared by football match cards and playback controls. */
fun Modifier.tapCard(onClick: () -> Unit): Modifier = composed {
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val scale = animateFloatAsState(if (pressed) 0.96f else 1f, label = "tapScale")
    this
        .clickable(interactionSource = interaction, indication = null, onClick = onClick)
        .graphicsLayer {
            scaleX = scale.value
            scaleY = scale.value
        }
}

/**
 * 卡片/列表行触控反馈（SPEC v1「手机按压态」）：surface 底 + 按压时 surfaceFocus 底，
 * **无 scale**——仅背景色过渡，用于整行比赛卡等信息承载面。
 */
fun Modifier.tapCardSurface(
    baseColor: Color,
    pressedColor: Color,
    shape: Shape,
    onClick: () -> Unit,
): Modifier = composed {
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val bg by animateColorAsState(
        targetValue = if (pressed) pressedColor else baseColor,
        label = "tapCardSurfaceBg",
    )
    this
        .clip(shape)
        .background(bg)
        .clickable(interactionSource = interaction, indication = null, onClick = onClick)
}

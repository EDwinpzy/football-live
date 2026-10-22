package com.qiubo.optimaltv.ui.components

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
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

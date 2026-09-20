package com.qiubo.optimaltv.ui.components

import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.FiniteAnimationSpec
import androidx.compose.animation.core.tween

/** TV 遥控交互的统一时序策略：短促、可预测，并与显示帧同步。 */
internal object TvInteractionMotion {
    private const val DIRECTION_REPEAT_INTERVAL_MS = 65L
    const val SCROLL_REVEAL_LAYOUT_WAIT_MS = 32L

    val focusScaleSpec: FiniteAnimationSpec<Float> = tween(
        durationMillis = 90,
        easing = FastOutSlowInEasing,
    )

    fun shouldProcessDirection(repeatCount: Int, elapsedMs: Long): Boolean =
        repeatCount == 0 || elapsedMs >= DIRECTION_REPEAT_INTERVAL_MS
}

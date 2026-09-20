package com.qiubo.optimaltv.ui.components

import androidx.compose.animation.core.AnimationVector1D
import androidx.compose.animation.core.VectorConverter
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TvInteractionMotionTest {

    @Test
    fun `初次方向键立即处理_长按重复在65毫秒起放行`() {
        assertTrue(TvInteractionMotion.shouldProcessDirection(repeatCount = 0, elapsedMs = 0L))
        assertFalse(TvInteractionMotion.shouldProcessDirection(repeatCount = 1, elapsedMs = 64L))
        assertTrue(TvInteractionMotion.shouldProcessDirection(repeatCount = 1, elapsedMs = 65L))
    }

    @Test
    fun `焦点缩放动画在100毫秒内收束_避免快速导航叠加长尾动画`() {
        val spec = TvInteractionMotion.focusScaleSpec.vectorize(Float.VectorConverter)
        val durationNanos = spec.getDurationNanos(
            initialValue = AnimationVector1D(1f),
            targetValue = AnimationVector1D(1.06f),
            initialVelocity = AnimationVector1D(0f),
        )

        assertEquals(90_000_000L, durationNanos)
    }

    @Test
    fun `跨屏滚动等待不超过两帧量级_避免方向键出现50毫秒固定停顿`() {
        assertEquals(32L, TvInteractionMotion.SCROLL_REVEAL_LAYOUT_WAIT_MS)
    }
}

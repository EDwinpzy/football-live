package com.qiubo.optimaltv.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class VlcRenderSizeTest {
    @Test
    fun ignoresUnmeasuredTextureViewUntilSurfaceHasSize() {
        assertNull(VlcRenderSize.fromViewSize(0, 1080))
        assertNull(VlcRenderSize.fromViewSize(1920, 0))
        assertNull(VlcRenderSize.fromViewSize(-1, 720))
    }

    @Test
    fun preservesActualSurfacePixelsAcrossNonDefaultDensities() {
        assertEquals(VlcRenderSize(1920, 1080), VlcRenderSize.fromViewSize(1920, 1080))
        assertEquals(VlcRenderSize(2400, 1080), VlcRenderSize.fromViewSize(2400, 1080))
    }

    @Test
    fun packsBothDimensionsWithoutLosingTheHeight() {
        val size = VlcRenderSize(1920, 1080)

        assertEquals((1920L shl 32) or 1080L, size.packed)
    }
}

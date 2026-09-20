package com.qiubo.optimaltv.ui.components

import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.geometry.Size
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class OtvNavNodeGeometryTest {

    @Test
    fun `裁边布局可作为当前焦点起点_但不得进入导航候选池`() {
        val node = OtvNav.Node(FocusRequester())
        val clipped = Rect(0f, 0f, 120f, 16f)
        val full = Rect(0f, -84f, 120f, 16f)

        OtvNav.reportNodeLayout(
            node = node,
            clippedBounds = clipped,
            fullBounds = full,
            layoutSize = Size(120f, 100f),
            stamp = 100L,
        )

        assertFalse(OtvNav.isNavigationCandidate(node))
        assertTrue(OtvNav.isFocusedNodeUsable(node))
        assertEquals(full, OtvNav.currentNodeOrigin(node))
    }

    @Test
    fun `完整节点移到裁边位置后立即退出候选池_不得沿用旧坐标`() {
        val node = OtvNav.Node(FocusRequester())
        val visible = Rect(40f, 40f, 160f, 140f)
        OtvNav.reportNodeLayout(
            node = node,
            clippedBounds = visible,
            fullBounds = visible,
            layoutSize = Size(120f, 100f),
            stamp = 100L,
        )
        assertTrue(OtvNav.isNavigationCandidate(node))

        val clipped = Rect(40f, 40f, 160f, 56f)
        val full = Rect(40f, -44f, 160f, 56f)
        OtvNav.reportNodeLayout(
            node = node,
            clippedBounds = clipped,
            fullBounds = full,
            layoutSize = Size(120f, 100f),
            stamp = 200L,
        )

        assertFalse(OtvNav.isNavigationCandidate(node))
        assertTrue(OtvNav.isFocusedNodeUsable(node))
        assertEquals(full, OtvNav.currentNodeOrigin(node))
    }

    @Test
    fun `完全离屏节点即使有未裁剪坐标也不可作为当前焦点起点`() {
        val node = OtvNav.Node(FocusRequester())
        OtvNav.reportNodeLayout(
            node = node,
            clippedBounds = Rect.Zero,
            fullBounds = Rect(0f, -100f, 120f, 0f),
            layoutSize = Size(120f, 100f),
            stamp = 100L,
        )

        assertFalse(OtvNav.isNavigationCandidate(node))
        assertFalse(OtvNav.isFocusedNodeUsable(node))
        assertEquals(Rect.Zero, OtvNav.currentNodeOrigin(node))
    }
}

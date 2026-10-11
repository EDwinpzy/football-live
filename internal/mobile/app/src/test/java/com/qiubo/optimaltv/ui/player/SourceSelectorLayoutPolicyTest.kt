package com.qiubo.optimaltv.ui.player

import org.junit.Assert.assertEquals
import org.junit.Test

class SourceSelectorLayoutPolicyTest {
    @Test
    fun keepsOnlyThePrefixThatFitsTwoRows() {
        assertEquals(
            4,
            SourceSelectorLayoutPolicy.visiblePrefixCount(
                chipWidthsPx = listOf(40, 50, 40, 50, 40),
                availableWidthPx = 100,
                spacingPx = 10,
            ),
        )
    }

    @Test
    fun wideChipUsesOneWholeRowAndDoesNotExposeTheThirdRow() {
        assertEquals(
            2,
            SourceSelectorLayoutPolicy.visiblePrefixCount(
                chipWidthsPx = listOf(180, 30, 80, 30),
                availableWidthPx = 100,
                spacingPx = 10,
            ),
        )
    }

    @Test
    fun allSourcesRemainVisibleWhenTheyFit() {
        assertEquals(
            3,
            SourceSelectorLayoutPolicy.visiblePrefixCount(
                chipWidthsPx = listOf(40, 40, 40),
                availableWidthPx = 100,
                spacingPx = 10,
            ),
        )
    }
}

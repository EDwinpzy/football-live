package com.qiubo.optimaltv.ui.search

import org.junit.Assert.assertEquals
import org.junit.Test

class SearchLayoutSpecTest {
    @Test
    fun tvosTopResultsUseLandscapeFourColumnCards() {
        assertEquals(4, SearchLayoutSpec.resultColumns)
        assertEquals(410f, SearchLayoutSpec.resultCardWidth, 0f)
        assertEquals(231f, SearchLayoutSpec.resultCardHeight, 0f)
        assertEquals(12f, SearchLayoutSpec.resultCardCornerRadius, 0f)
    }

    @Test
    fun tvosSearchChromeMatchesReferenceCanvas() {
        assertEquals(70f, SearchLayoutSpec.pageSidePadding, 0f)
        assertEquals(295f, SearchLayoutSpec.keyboardHeight, 0f)
        assertEquals(0.3f, SearchLayoutSpec.dividerOpacity, 0f)
    }
}

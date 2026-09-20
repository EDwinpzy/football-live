package com.qiubo.optimaltv.ui.search

import org.junit.Assert.assertEquals
import org.junit.Test

class SearchKeyboardLayoutTest {
    @Test
    fun actionKeyTextUsesAnExplicitCenteredLineHeight() {
        assertEquals(64f, SearchKeyboardLayout.actionKeyHeight, 0f)
        assertEquals(185f, SearchKeyboardLayout.actionKeyY, 0f)
        assertEquals(14f, SearchKeyboardLayout.actionKeyRadius, 0f)
        assertEquals(20f, SearchKeyboardLayout.actionKeyLineHeight, 0f)
        assertEquals(22f, SearchKeyboardLayout.actionKeyVerticalInset, 0f)
        assertEquals(6f, SearchKeyboardLayout.resultTabVerticalPadding, 0f)
    }
}

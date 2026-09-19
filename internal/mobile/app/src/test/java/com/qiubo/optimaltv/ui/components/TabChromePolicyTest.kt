package com.qiubo.optimaltv.ui.components

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TabChromePolicyTest {
    @Test
    fun onlyAbsoluteTopKeepsTabBarVisible() {
        assertFalse(isScrolledFromTop(0, 0))
        assertTrue(isScrolledFromTop(0, 1))
        assertTrue(isScrolledFromTop(1, 0))
    }
}

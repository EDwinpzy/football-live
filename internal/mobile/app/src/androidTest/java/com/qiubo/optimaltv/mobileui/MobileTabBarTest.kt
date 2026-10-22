package com.qiubo.optimaltv.mobileui

import androidx.compose.ui.test.assertHasNoClickAction
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.assertDoesNotExist
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.qiubo.optimaltv.ui.components.MainTabBar
import com.qiubo.optimaltv.ui.components.MobileUiTags
import com.qiubo.optimaltv.ui.components.isScrolledFromTop
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test

class MobileTabBarTest {
    @get:Rule
    val rule = createAndroidComposeRule<MobileUiHarnessActivity>()

    @Before
    fun resetScene() {
        MobileUiHarness.scene = null
    }

    @Test
    fun hiddenTabBarDoesNotExposeTouchActions() {
        MobileUiHarness.scene = {
            MainTabBar(currentKey = "live", onSelect = {}, hidden = true)
        }
        rule.waitForIdle()

        rule.onNodeWithText("足球直播").assertHasNoClickAction()
    }

    @Test
    fun footballIsTheOnlyTabAndSearchIsAbsent() {
        var selected = ""
        MobileUiHarness.scene = {
            MainTabBar(currentKey = "live", onSelect = { selected = it })
        }
        rule.waitForIdle()

        rule.onNodeWithTag(MobileUiTags.mainTab("live")).assertIsSelected().performClick()
        rule.onNodeWithTag(MobileUiTags.mainTab("vod")).assertDoesNotExist()
        rule.onNodeWithTag("mobile-tab-search").assertDoesNotExist()
        rule.runOnIdle { assertEquals("live", selected) }
    }

    @Test
    fun displayOnlyTabBarDoesNotExposeTouchActions() {
        MobileUiHarness.scene = {
            MainTabBar("live", {}, interactive = false)
        }
        rule.waitForIdle()

        rule.onNodeWithTag(MobileUiTags.mainTab("live")).assertHasNoClickAction()
    }

    @Test
    fun anyNonZeroScrollOffsetHidesTheTabBar() {
        assertFalse(isScrolledFromTop(0, 0))
        assertTrue(isScrolledFromTop(0, 1))
        assertTrue(isScrolledFromTop(1, 0))
    }
}

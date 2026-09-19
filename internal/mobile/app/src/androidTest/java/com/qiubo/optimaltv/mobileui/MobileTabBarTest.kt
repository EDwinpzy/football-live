package com.qiubo.optimaltv.mobileui

import androidx.compose.ui.test.assertHasNoClickAction
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.assertIsNotSelected
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
            MainTabBar(
                currentKey = "vod",
                onSelect = {},
                onSearch = {},
                hidden = true,
            )
        }
        rule.waitForIdle()

        rule.onNodeWithText("影视").assertHasNoClickAction()
    }

    @Test
    fun tabAndSearchClicksEmitNavigationIntents() {
        var selected = ""
        var searchClicks = 0
        MobileUiHarness.scene = {
            MainTabBar(
                currentKey = "vod",
                onSelect = { selected = it },
                onSearch = { searchClicks++ },
            )
        }
        rule.waitForIdle()

        rule.onNodeWithTag(MobileUiTags.mainTab("vod")).assertIsSelected()
        rule.onNodeWithTag(MobileUiTags.mainTab("tv")).assertIsNotSelected().performClick()
        rule.runOnIdle { assertEquals("tv", selected) }
        rule.onNodeWithTag(MobileUiTags.SearchTab).performClick()
        rule.runOnIdle { assertEquals(1, searchClicks) }
    }

    @Test
    fun displayOnlyTabBarDoesNotExposeTouchActions() {
        MobileUiHarness.scene = {
            MainTabBar("vod", {}, {}, interactive = false)
        }
        rule.waitForIdle()

        rule.onNodeWithTag(MobileUiTags.mainTab("vod")).assertHasNoClickAction()
        rule.onNodeWithTag(MobileUiTags.SearchTab).assertHasNoClickAction()
    }

    @Test
    fun anyNonZeroScrollOffsetHidesTheTabBar() {
        assertFalse(isScrolledFromTop(0, 0))
        assertTrue(isScrolledFromTop(0, 1))
        assertTrue(isScrolledFromTop(1, 0))
    }
}

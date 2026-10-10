package com.qiubo.optimaltv.mobileui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.unit.dp
import com.qiubo.optimaltv.ui.player.ErrorWidget
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Rule
import org.junit.Test

class PlayerErrorWidgetTest {
    @get:Rule
    val rule = createAndroidComposeRule<MobileUiHarnessActivity>()

    @Before
    fun resetScene() {
        MobileUiHarness.scene = null
    }

    @Test
    fun portraitFailureActionsRemainVisibleAboveTheSixteenByNineVideoArea() {
        var retryCount = 0
        var backCount = 0
        var sourceCount = 0
        MobileUiHarness.scene = {
            Box(Modifier.fillMaxSize()) {
                // Recreate the portrait player layout: a 16:9 video area plus a full-screen
                // failure overlay as its sibling.
                Box(
                    Modifier
                        .fillMaxWidth()
                        .aspectRatio(16f / 9f)
                        .align(Alignment.TopCenter)
                        .background(Color.DarkGray),
                )
                ErrorWidget(
                    message = "未找到可播放片源，请选择其他线路后重试。",
                    onRetry = { retryCount++ },
                    onBack = { backCount++ },
                    onMore = { sourceCount++ },
                    showMore = true,
                )
            }
        }
        rule.waitForIdle()

        rule.onNodeWithText("无法播放").assertIsDisplayed()
        rule.onNodeWithText("重试").assertIsDisplayed().performClick()
        rule.onNodeWithText("返回").assertIsDisplayed().performClick()
        rule.onNodeWithText("线路").assertIsDisplayed().performClick()
        rule.runOnIdle {
            assertEquals(1, retryCount)
            assertEquals(1, backCount)
            assertEquals(1, sourceCount)
        }
    }

    @Test
    fun failureActionsCanBeScrolledIntoViewInShortLandscapeViewport() {
        var retryCount = 0
        var backCount = 0
        var sourceCount = 0
        MobileUiHarness.scene = {
            Box(Modifier.fillMaxWidth().height(260.dp)) {
                ErrorWidget(
                    message = "播放源暂时不可用。",
                    onRetry = { retryCount++ },
                    onBack = { backCount++ },
                    onMore = { sourceCount++ },
                    showMore = true,
                )
            }
        }
        rule.waitForIdle()

        rule.onNodeWithText("重试").performScrollTo().assertIsDisplayed().performClick()
        rule.onNodeWithText("返回").performScrollTo().assertIsDisplayed().performClick()
        rule.onNodeWithText("线路").performScrollTo().assertIsDisplayed().performClick()
        rule.runOnIdle {
            assertEquals(1, retryCount)
            assertEquals(1, backCount)
            assertEquals(1, sourceCount)
        }
    }
}

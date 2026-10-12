package com.qiubo.optimaltv.mobileui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.ui.player.LiveSourcePicker
import com.qiubo.optimaltv.ui.player.PlayerUiState
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Rule
import org.junit.Test

class PlayerLiveFailureControlsTest {
    @get:Rule
    val rule = createAndroidComposeRule<MobileUiHarnessActivity>()

    @Before
    fun resetScene() {
        MobileUiHarness.scene = null
    }

    @Test
    fun liveFailureStaysInlineAndKeepsRetryAndSourceSelectionAvailable() {
        var retryCount = 0
        var selectedLine = -1
        val ui = PlayerUiState(
            isLive = true,
            fatalMsg = "当前信号源暂时无法播放",
            // Deliberately false: a fatal live state must keep the source controls visible.
            srcPickerVisible = false,
            lines = listOf(
                LineInfo("live:match", "线路 A2", "https://example.invalid/a2"),
                LineInfo("live:match", "线路 Q3", "https://example.invalid/q3"),
            ),
        )
        MobileUiHarness.scene = {
            Box(Modifier.fillMaxSize().background(Color.Black)) {
                LiveSourcePicker(
                    ui = ui,
                    onSelectLine = { selectedLine = it },
                    onRetry = { retryCount++ },
                    modifier = Modifier.align(Alignment.BottomCenter),
                    onTick = {},
                )
            }
        }
        rule.waitForIdle()

        rule.onNodeWithTag("live-playback-error-title").assertIsDisplayed()
        rule.onNodeWithText("当前信号源暂时无法播放").assertIsDisplayed()
        rule.onNodeWithText("信号源").assertIsDisplayed()
        rule.onNodeWithText("线路 A2").assertIsDisplayed()
        rule.onNodeWithText("线路 Q3").assertIsDisplayed()
        savePlayerUiEvidence("live-failure-controls", rule.onRoot().captureToImage().asAndroidBitmap())
        rule.onNodeWithTag("live-playback-retry").assertIsDisplayed().performClick()
        rule.onNodeWithTag("live-source-1").assertIsDisplayed().performClick()

        rule.runOnIdle {
            assertEquals(1, retryCount)
            assertEquals(1, selectedLine)
        }
    }
}

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
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.dp
import com.qiubo.optimaltv.data.model.LiveSourceLine
import com.qiubo.optimaltv.ui.player.LiveSourcePicker
import com.qiubo.optimaltv.ui.player.PlayerUiState
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Rule
import org.junit.Test

/** 回归移动端真实播放失败控件，避免测试引用已删除的旧 ErrorWidget。 */
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
        var selectedLine = -1
        val ui = PlayerUiState(
            fatalMsg = "未找到可播放片源，请选择其他线路后重试。",
            lines = listOf(
                LiveSourceLine("线路 A2", "https://example.invalid/a2"),
                LiveSourceLine("线路 Q3", "https://example.invalid/q3"),
            ),
        )
        MobileUiHarness.scene = {
            Box(Modifier.fillMaxSize()) {
                Box(
                    Modifier
                        .fillMaxWidth()
                        .aspectRatio(16f / 9f)
                        .align(Alignment.TopCenter)
                        .background(Color.DarkGray),
                )
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

        rule.onNodeWithText("无法播放").assertIsDisplayed()
        rule.onNodeWithText("重新检测").assertIsDisplayed().performClick()
        rule.onNodeWithText("线路 Q3").assertIsDisplayed().performClick()
        rule.runOnIdle {
            assertEquals(1, retryCount)
            assertEquals(1, selectedLine)
        }
    }

    @Test
    fun failureActionsRemainAvailableInShortLandscapeViewport() {
        var retryCount = 0
        val ui = PlayerUiState(
            fatalMsg = "播放源暂时不可用。",
            lines = listOf(LiveSourceLine("线路 A2", "https://example.invalid/a2")),
        )
        MobileUiHarness.scene = {
            Box(Modifier.fillMaxWidth().height(260.dp)) {
                LiveSourcePicker(
                    ui = ui,
                    onSelectLine = {},
                    onRetry = { retryCount++ },
                    modifier = Modifier.align(Alignment.BottomCenter),
                    onTick = {},
                )
            }
        }
        rule.waitForIdle()

        rule.onNodeWithText("重新检测").assertIsDisplayed().performClick()
        rule.onNodeWithTag("live-source-0").assertIsDisplayed()
        rule.runOnIdle { assertEquals(1, retryCount) }
    }
}

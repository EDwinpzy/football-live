package com.qiubo.optimaltv.mobileui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.width
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.InputMode
import androidx.compose.ui.platform.LocalInputModeManager
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.graphics.asAndroidBitmap
import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.ui.player.PlayerMoreSections
import com.qiubo.optimaltv.ui.player.PlayerUiState
import org.junit.Before
import org.junit.Rule
import org.junit.Test

class PlayerSourceSelectorContractTest {
    @get:Rule
    val rule = createAndroidComposeRule<MobileUiHarnessActivity>()

    @Before
    fun resetScene() {
        MobileUiHarness.scene = null
    }

    @Test
    @OptIn(ExperimentalComposeUiApi::class)
    fun sourceListStartsCollapsedAndArrowKeepsFocusWhileExpandingAndCollapsing() {
        val lines = (1..20).map { index -> LineInfo("vod:$index", "线路$index", "") }
        val ui = PlayerUiState(
            title = "线路选择测试",
            isMovie = true,
            lines = lines,
            activeLine = lines.lastIndex,
        )
        MobileUiHarness.scene = {
            val inputModeManager = LocalInputModeManager.current
            LaunchedEffect(inputModeManager) {
                inputModeManager.requestInputMode(InputMode.Keyboard)
            }
            Column(Modifier.width(320.dp).verticalScroll(rememberScrollState())) {
                PlayerMoreSections(
                    ui = ui,
                    onPickEpisode = {},
                    onSwitchLine = {},
                    onSetSpeed = {},
                    onSetAspect = {},
                    onStartCast = {},
                    onStopCast = {},
                    onRescanCast = {},
                    autoFocusSource = true,
                )
            }
        }
        rule.waitForIdle()

        val collapsedArrow = rule.onNodeWithContentDescription("展开线路，另有", substring = true)
            .assertIsDisplayed()
            .assertIsFocused()
        rule.onNodeWithText("线路20 · 当前").assertDoesNotExist()
        savePlayerUiEvidence("source-selector-collapsed", rule.onRoot().captureToImage().asAndroidBitmap())
        collapsedArrow.performClick()
        rule.onNodeWithText("线路20 · 当前").performScrollTo().assertIsDisplayed()
        savePlayerUiEvidence("source-selector-expanded", rule.onRoot().captureToImage().asAndroidBitmap())
        rule.onNodeWithContentDescription("收起线路").performScrollTo().assertIsDisplayed().performClick()

        rule.onNodeWithContentDescription("展开线路，另有", substring = true).assertIsFocused()
        rule.onNodeWithText("线路20 · 当前").assertDoesNotExist()
    }
}

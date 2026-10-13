package com.qiubo.optimaltv.mobileui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.width
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
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToIndex
import androidx.compose.ui.unit.dp
import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.ui.player.PlayerMoreSections
import com.qiubo.optimaltv.ui.player.PlayerUiState
import org.junit.Assert.assertEquals
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
    fun vodSourcesStayInFixedHeightScrollableListAndRevealTheActiveLine() {
        val lines = (1..20).map { index -> LineInfo("vod:$index", "好好看·线路$index", "") }
        var selectedLine = -1
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
                    onSwitchLine = { selectedLine = it },
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

        rule.onNodeWithText("好好看").assertIsDisplayed()
        rule.onNodeWithText("线路20 · 当前").assertIsDisplayed().assertIsFocused()
        rule.onNodeWithContentDescription("展开线路，另有", substring = true).assertDoesNotExist()

        val listBounds = rule.onNodeWithTag("vod-source-list").fetchSemanticsNode().boundsInRoot
        val expectedHeight = with(rule.density) { 220.dp.toPx() }
        assertEquals("片源列表高度固定", expectedHeight, listBounds.height, 1.1f)

        rule.onNodeWithTag("vod-source-list").performScrollToIndex(7)
        rule.onNodeWithText("线路8").assertIsDisplayed().performClick()
        rule.runOnIdle { assertEquals("滚动后仍回传原始全局索引", 7, selectedLine) }
    }

    @Test
    fun providerTabsBrowseGroupsAndSourceSelectionKeepsGlobalIndex() {
        var selectedLine = -1
        val ui = PlayerUiState(
            title = "来源分组测试",
            isMovie = true,
            lines = listOf(
                LineInfo("vod:1", "好好看·正片", ""),
                LineInfo("vod:2", "好好看·备用", ""),
                LineInfo("vod:3", "非凡资源·高清", ""),
                LineInfo("vod:4", "非凡资源·蓝光", ""),
                LineInfo("vod:5", "非凡资源·超清", ""),
            ),
            activeLine = 0,
        )
        MobileUiHarness.scene = {
            Column(Modifier.width(320.dp).verticalScroll(rememberScrollState())) {
                PlayerMoreSections(
                    ui = ui,
                    onPickEpisode = {},
                    onSwitchLine = { selectedLine = it },
                    onSetSpeed = {},
                    onSetAspect = {},
                    onStartCast = {},
                    onStopCast = {},
                    onRescanCast = {},
                )
            }
        }
        rule.waitForIdle()

        rule.onNodeWithText("好好看").assertIsDisplayed()
        rule.onNodeWithText("正片 · 当前").assertIsDisplayed()
        rule.onNodeWithText("非凡资源").performClick()
        rule.onNodeWithText("高清").assertIsDisplayed()
        rule.onNodeWithText("超清").performScrollTo().assertIsDisplayed().performClick()
        rule.runOnIdle { assertEquals("切换分组只浏览；选择线路回传原始索引", 4, selectedLine) }
    }

    @Test
    fun hhkanReferencesGroupUnprefixedRoutesWithoutReclassifyingOtherSources() {
        var selectedLine = -1
        val hhkan = listOf("超清2", "FF线路", "HN线路").mapIndexed { index, label ->
            LineInfo("hhkan:374249", label, "", "hhkan:374249|source$index:content:$index|0")
        }
        val ui = PlayerUiState(
            title = "好好看线路测试",
            isMovie = true,
            lines = hhkan + LineInfo("api:7", "未分组线路", "", "api:7|channel:4"),
            activeLine = 0,
        )
        MobileUiHarness.scene = {
            Column(Modifier.width(320.dp).verticalScroll(rememberScrollState())) {
                PlayerMoreSections(
                    ui = ui,
                    onPickEpisode = {},
                    onSwitchLine = { selectedLine = it },
                    onSetSpeed = {},
                    onSetAspect = {},
                    onStartCast = {},
                    onStopCast = {},
                    onRescanCast = {},
                )
            }
        }
        rule.waitForIdle()

        rule.onNodeWithText("好好看").assertIsDisplayed()
        rule.onNodeWithText("超清2 · 当前").assertIsDisplayed()
        rule.onNodeWithText("HN线路").performScrollTo().assertIsDisplayed().performClick()
        rule.runOnIdle { assertEquals("好好看来源仍使用原始线路索引", 2, selectedLine) }

        rule.onNodeWithText("其他信号源").performClick()
        rule.onNodeWithText("未分组线路").assertIsDisplayed()
    }
}

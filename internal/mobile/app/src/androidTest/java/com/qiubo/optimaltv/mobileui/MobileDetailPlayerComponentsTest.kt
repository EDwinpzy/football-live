package com.qiubo.optimaltv.mobileui

import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.input.InputMode
import androidx.compose.ui.platform.LocalInputModeManager
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import com.qiubo.optimaltv.data.model.Episode
import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.ui.components.DetailActionButton
import com.qiubo.optimaltv.ui.components.MobileUiTags
import com.qiubo.optimaltv.ui.detail.shouldShowEpisodePicker
import com.qiubo.optimaltv.ui.player.PlayerMoreSections
import com.qiubo.optimaltv.ui.player.PlayerUiState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test

class MobileDetailPlayerComponentsTest {
    @get:Rule
    val rule = createAndroidComposeRule<MobileUiHarnessActivity>()

    @Before
    fun resetScene() {
        MobileUiHarness.scene = null
    }

    @Test
    fun detailButtonsDispatchIndependentActions() {
        var play = 0
        var favorite = 0
        MobileUiHarness.scene = {
            Row(Modifier.fillMaxWidth()) {
                DetailActionButton("立即播放", true, true, 0.7f, MobileUiTags.DetailPlay) { play++ }
                DetailActionButton("☆ 收藏", false, true, 0.7f, MobileUiTags.DetailFavorite) { favorite++ }
            }
        }
        rule.waitForIdle()

        rule.onNodeWithTag(MobileUiTags.DetailPlay).performClick()
        rule.onNodeWithTag(MobileUiTags.DetailFavorite).performClick()
        rule.runOnIdle {
            assertEquals(1, play)
            assertEquals(1, favorite)
        }
    }

    @Test
    fun moviePlaybackVersionsStayHiddenWhileSeriesEpisodesRemain() {
        val versions = listOf(
            Episode(0, "HD中字", ""),
            Episode(1, "HD国语", ""),
            Episode(2, "正片", ""),
        )
        val movie = VodItem("movie", "hhkan", "电影", "hhkan:1", episodes = versions)
        val series = VodItem("series", "hhkan", "剧集", "hhkan:2", episodes = versions)

        assertFalse(shouldShowEpisodePicker(movie))
        assertTrue(shouldShowEpisodePicker(series))
    }

    /** 竖屏内容区（2026-09-19 v2：「更多」区段平铺）——选集/换源/倍速/画幅点击全走回调，
     *  打开即自动触发一次投屏扫描。 */
    @Test
    fun portraitMoreSectionsDispatchEpisodeLineSpeedAspect() {
        var ep = -1
        var line = -1
        var speed = 0f
        var aspect = -1
        var rescan = 0
        val ui = PlayerUiState(
            title = "自动化测试影片",
            meta = "2025 / 中国 / 剧情",
            desc = "简介",
            epCount = 12,
            epIndex = 2,
            isMovie = false,
            lines = listOf(LineInfo("a", "线路A", ""), LineInfo("b", "线路B", "")),
            activeLine = 0,
            engineName = "ExoPlayer",
            speed = 1.0f,
            aspectOrdinal = 0,
        )
        MobileUiHarness.scene = {
            Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
                PlayerMoreSections(
                    ui = ui,
                    onPickEpisode = { ep = it },
                    onSwitchLine = { line = it },
                    onSetSpeed = { speed = it },
                    onSetAspect = { aspect = it },
                    onStartCast = {},
                    onStopCast = {},
                    onRescanCast = { rescan++ },
                )
            }
        }
        rule.waitForIdle()

        rule.onNodeWithText("选择集数").assertExists()
        rule.onNodeWithText("5").performClick()
        rule.onNodeWithText("线路B").performClick()
        rule.onNodeWithText("1.5x").performScrollTo().performClick()
        rule.onNodeWithText("4:3").performScrollTo().performClick()
        rule.runOnIdle {
            assertEquals(4, ep)
            assertEquals(1, line)
            assertEquals(1.5f, speed, 0.001f)
            assertEquals(3, aspect)
            assertTrue("打开区段应自动扫描投屏设备", rescan >= 1)
        }
    }

    @Test
    @OptIn(ExperimentalComposeUiApi::class)
    fun sourceSelectorPrecedesSynopsisAndEpisodesAndReceivesDefaultFocus() {
        val synopsis = "这是一段很长的影片简介。".repeat(80)
        val ui = PlayerUiState(
            title = "自动化测试影片",
            desc = synopsis,
            epCount = 12,
            isMovie = false,
            lines = listOf(LineInfo("douban:456", "高清线路甲", "", "456|source:content|0")),
            activeLine = 0,
            sourceStatus = com.qiubo.optimaltv.ui.player.PlayerSourceStatus.READY,
        )
        MobileUiHarness.scene = {
            val inputModeManager = LocalInputModeManager.current
            LaunchedEffect(inputModeManager) {
                inputModeManager.requestInputMode(InputMode.Keyboard)
            }
            Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
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

        val source = rule.onNodeWithText("高清线路甲 · 当前").assertIsFocused()
            .fetchSemanticsNode().boundsInRoot
        val description = rule.onNodeWithText(synopsis).fetchSemanticsNode().boundsInRoot
        val episodes = rule.onNodeWithText("选择集数").fetchSemanticsNode().boundsInRoot
        assertTrue("线路必须在简介前", source.top < description.top)
        assertTrue("线路必须在选集前", source.top < episodes.top)
    }

    /** 电影不渲染选集区（需求⑨），直播不渲染倍速区。 */
    @Test
    fun moreSectionsHideEpisodePickerForMovieAndSpeedForLive() {
        MobileUiHarness.scene = {
            PlayerMoreSections(
                ui = PlayerUiState(title = "电影", isMovie = true, epCount = 1, speed = 1.0f),
                onPickEpisode = {}, onSwitchLine = {}, onSetSpeed = {}, onSetAspect = {},
                onStartCast = {}, onStopCast = {}, onRescanCast = {},
            )
        }
        rule.waitForIdle()
        rule.onNodeWithText("选择集数").assertDoesNotExist()
        rule.onNodeWithText("倍速").assertExists()
        rule.onNodeWithText("画幅").assertExists()

        MobileUiHarness.scene = {
            PlayerMoreSections(
                ui = PlayerUiState(title = "CCTV-5", isLive = true, speed = 1.0f),
                onPickEpisode = {}, onSwitchLine = {}, onSetSpeed = {}, onSetAspect = {},
                onStartCast = {}, onStopCast = {}, onRescanCast = {},
            )
        }
        rule.waitForIdle()
        rule.onNodeWithText("倍速").assertDoesNotExist()
        rule.onNodeWithText("画幅").assertExists()
    }
}

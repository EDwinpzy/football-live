package com.qiubo.optimaltv.focus

import android.graphics.Bitmap
import android.view.KeyEvent
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.test.platform.app.InstrumentationRegistry
import com.qiubo.optimaltv.data.db.AppDb
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.data.repo.VodRepository
import com.qiubo.optimaltv.ui.components.OtvNav
import com.qiubo.optimaltv.ui.player.MorePanel
import com.qiubo.optimaltv.ui.player.PlayerUiState
import com.qiubo.optimaltv.ui.player.PlayerViewModel
import com.qiubo.optimaltv.ui.theme.OptimalTvTheme
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import java.io.File

/** Render the production sidebar, including incomplete rows and episode selection. */
class PlayerEpisodeWidthTest {
    @get:Rule val rule = createAndroidComposeRule<FocusHarnessActivity>()

    @Before fun reset() {
        Harness.scene = null
        OtvNav.resetForTest()
    }

    private fun verify(count: Int) {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val settings = SettingsStore(context)
        val vm = PlayerViewModel("hhkan:11", 0,
            VodRepository(settings, OkHttpClient(), context), AppDb.get(context), settings,
            autoBoot = false)
        var picked = -1
        Harness.scene = {
            OptimalTvTheme {
                MorePanel(PlayerUiState(title = "选集等宽验证", epCount = count,
                    meta = "电视剧 · $count 集", desc = "最后一行保留五列，所有选集按钮保持相同宽度。"),
                    vm, onPickEpisode = { picked = it })
            }
        }
        rule.waitForIdle()
        val first = rule.onNodeWithText("1").fetchSemanticsNode().boundsInRoot
        for (episode in 1..count) {
            val bounds = rule.onNodeWithText(episode.toString()).assertIsDisplayed()
                .fetchSemanticsNode().boundsInRoot
            assertEquals("episode $episode width", first.width, bounds.width, 1f)
        }
        val last = rule.onNodeWithText(count.toString()).fetchSemanticsNode().boundsInRoot
        if (count == 11) assertEquals("last row starts in first column", first.left, last.left, 1f)
        rule.onNodeWithText(count.toString()).performClick()
        rule.runOnIdle { assertEquals(count - 1, picked) }
        if (count == 11) {
            InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_DPAD_UP)
            rule.waitForIdle()
        }
        val screenshot = rule.onRoot().captureToImage().asAndroidBitmap()
        File(context.filesDir, "episode-width-$count.png").outputStream().use {
            screenshot.compress(Bitmap.CompressFormat.PNG, 100, it)
        }
    }

    @Test fun elevenEpisodesKeepFiveColumnWidths() = verify(11)
    @Test fun sixteenEpisodesAreVisibleAndSelectable() = verify(16)
    @Test fun nineteenEpisodesKeepFiveColumnWidths() = verify(19)
}

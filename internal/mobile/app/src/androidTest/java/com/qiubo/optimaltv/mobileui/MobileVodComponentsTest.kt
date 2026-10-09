package com.qiubo.optimaltv.mobileui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.test.assertContentDescriptionEquals
import androidx.compose.ui.test.assertHasClickAction
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeLeft
import androidx.compose.ui.unit.dp
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.ui.components.MobileBackButton
import com.qiubo.optimaltv.ui.components.MobileUiTags
import com.qiubo.optimaltv.ui.components.MobileVodCard
import com.qiubo.optimaltv.ui.components.VodCategorySelector
import com.qiubo.optimaltv.ui.components.VodFilterRow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test

class MobileVodComponentsTest {
    @get:Rule
    val rule = createAndroidComposeRule<MobileUiHarnessActivity>()

    private val categories = listOf(
        "hhkan:1" to "电影",
        "hhkan:2" to "电视剧",
        "hhkan:3" to "动漫",
        "hhkan:4" to "综艺",
        "hhkan:6" to "短剧",
        "" to "全部 ›",
    )

    private val fixture = VodItem(
        id = "fixture-1",
        sourceId = "hhkan",
        title = "自动化测试影片",
        categoryId = "hhkan:1",
        year = "2025",
        rating = 8.2,
        remark = "HD中字",
    )

    @Before
    fun resetScene() {
        MobileUiHarness.scene = null
    }

    private fun bounds(tag: String): Rect =
        rule.onNodeWithTag(tag).fetchSemanticsNode().boundsInRoot

    @Test
    fun backButtonIsAccessibleAndClickable() {
        var clicks = 0
        MobileUiHarness.scene = { MobileBackButton { clicks++ } }
        rule.waitForIdle()

        rule.onNodeWithTag(MobileUiTags.BackButton)
            .assertContentDescriptionEquals("返回")
            .assertHasClickAction()
            .performClick()
        rule.runOnIdle { assertEquals(1, clicks) }
    }

    @Test
    fun portraitCategoriesAreThreeColumnsByTwoRows() {
        MobileUiHarness.scene = {
            Box(Modifier.width(360.dp)) {
                VodCategorySelector(categories, "hhkan:1", true, 0.5f, 20.dp, {}, {})
            }
        }
        rule.waitForIdle()

        val firstRow = categories.take(3).map { bounds(MobileUiTags.category(it.first)) }
        val secondRow = categories.drop(3).map { bounds(MobileUiTags.category(it.first)) }
        assertTrue(firstRow.all { kotlin.math.abs(it.top - firstRow.first().top) < 1f })
        assertTrue(secondRow.all { kotlin.math.abs(it.top - secondRow.first().top) < 1f })
        assertTrue(secondRow.first().top > firstRow.first().bottom)
        assertTrue(firstRow[0].left < firstRow[1].left && firstRow[1].left < firstRow[2].left)
    }

    @Test
    fun landscapeCategoriesStayInOneRow() {
        MobileUiHarness.scene = {
            Box(Modifier.width(900.dp)) {
                VodCategorySelector(categories, "hhkan:1", false, 0.5f, 20.dp, {}, {})
            }
        }
        rule.waitForIdle()

        val row = categories.map { bounds(MobileUiTags.category(it.first)) }
        assertTrue(row.all { kotlin.math.abs(it.top - row.first().top) < 1f })
    }

    @Test
    fun categorySelectionAndAllHaveSeparateActions() {
        var allClicks = 0
        MobileUiHarness.scene = {
            var selected by remember { mutableStateOf("hhkan:1") }
            VodCategorySelector(
                categories = categories,
                selectedId = selected,
                portrait = true,
                s = 0.5f,
                sidePadding = 20.dp,
                onSelect = { selected = it },
                onAll = { allClicks++ },
            )
        }
        rule.waitForIdle()

        rule.onNodeWithTag(MobileUiTags.category("hhkan:2")).performClick().assertIsSelected()
        rule.onNodeWithTag(MobileUiTags.category("")).performClick()
        rule.runOnIdle { assertEquals(1, allClicks) }
    }

    @Test
    fun portraitFilterRowScrollsHorizontallyAndRemainsTappable() {
        val chips = listOf("" to "全部") + (1..8).map { "$it" to "选项$it" }
        MobileUiHarness.scene = {
            Box(Modifier.width(250.dp)) {
                var selected by remember { mutableStateOf("") }
                VodFilterRow("类型", chips, selected, 0.8f, true) { selected = it }
            }
        }
        rule.waitForIdle()

        val row = rule.onNodeWithContentDescription("类型")
        val first = bounds(MobileUiTags.filter("类型", ""))
        val last = bounds(MobileUiTags.filter("类型", "8"))
        assertTrue("横向筛选项应保持同一行", kotlin.math.abs(last.top - first.top) < 1f)

        repeat(3) { row.performTouchInput { swipeLeft() } }
        rule.waitForIdle()
        val rowBounds = row.fetchSemanticsNode().boundsInRoot
        val lastAfterScroll = bounds(MobileUiTags.filter("类型", "8"))
        assertTrue("末尾筛选项应可滚入触控区域", lastAfterScroll.left >= rowBounds.left)
        assertTrue("末尾筛选项应可完整触达", lastAfterScroll.right <= rowBounds.right)
        rule.onNodeWithTag(MobileUiTags.filter("类型", "8")).performClick().assertIsSelected()
    }

    @Test
    fun portraitFilterTextFitsInsideItsTouchTarget() {
        val chips = listOf("" to "全部") + (1..5).map { "$it" to "选项$it" }
        MobileUiHarness.scene = {
            Box(Modifier.width(180.dp)) {
                VodFilterRow("类型", chips, "", 0.8f, true) {}
            }
        }
        rule.waitForIdle()

        val first = bounds(MobileUiTags.filter("类型", ""))
        rule.onNodeWithTag(MobileUiTags.filter("类型", "3")).performScrollTo()
        rule.waitForIdle()
        val third = bounds(MobileUiTags.filter("类型", "3"))
        val label = rule.onNodeWithText("选项3", useUnmergedTree = true).fetchSemanticsNode().boundsInRoot
        val density = rule.activity.resources.displayMetrics.density
        assertTrue("横向筛选项应处在同一触控行", kotlin.math.abs(third.top - first.top) < 1f)
        assertTrue("竖屏胶囊高度应匹配当前 40dp 设计尺寸", first.height / density in 30f..34f)
        assertTrue("筛选文字应完整落在胶囊触控区内", label.top >= third.top && label.bottom <= third.bottom)
        assertTrue("筛选文字行盒不能撑满并裁切胶囊", label.height < third.height)
    }

    @Test
    fun filterChipUsesCompactPillHeight() {
        MobileUiHarness.scene = {
            Box(Modifier.width(250.dp)) {
                VodFilterRow("类型", listOf("" to "全部"), "", 0.8f, true) {}
            }
        }
        rule.waitForIdle()

        val pill = bounds(MobileUiTags.filter("类型", ""))
        val density = rule.activity.resources.displayMetrics.density
        assertTrue("竖屏胶囊应保留当前可读触控高度", pill.height / density in 30f..34f)
        assertTrue("选中筛选项应呈扁圆形", pill.width > pill.height * 1.2f)
    }

    @Test
    fun compactPosterCardHidesRatingAndHandlesTap() {
        var clicks = 0
        MobileUiHarness.scene = {
            Box(Modifier.width(110.dp)) {
                MobileVodCard(fixture, 0.5f, true) { clicks++ }
            }
        }
        rule.waitForIdle()

        rule.onNodeWithText("自动化测试影片").assertTextEquals("自动化测试影片")
        rule.onNodeWithText("HD中字").assertTextEquals("HD中字")
        rule.onNodeWithText("2025  ★ 8.2").assertDoesNotExist()
        rule.onNodeWithTag(MobileUiTags.cardPoster(fixture.id)).performClick()
        rule.runOnIdle { assertEquals(1, clicks) }
    }

    @Test
    fun regularPosterCardShowsOnlyTitleAndRemark() {
        MobileUiHarness.scene = {
            Box(Modifier.width(180.dp)) {
                MobileVodCard(fixture, 0.7f, false) {}
            }
        }
        rule.waitForIdle()

        rule.onNodeWithText("自动化测试影片").assertTextEquals("自动化测试影片")
        rule.onNodeWithText("HD中字").assertTextEquals("HD中字")
        rule.onNodeWithText("2025  ★ 8.2").assertDoesNotExist()
    }

    @Test
    fun searchStyleCardNeverAddsRating() {
        MobileUiHarness.scene = {
            Box(Modifier.width(180.dp)) {
                MobileVodCard(fixture, 0.7f, false) {}
            }
        }
        rule.waitForIdle()

        rule.onNodeWithText("2025  ★ 8.2").assertDoesNotExist()
        rule.onNodeWithText("★ 8.2").assertDoesNotExist()
    }
}

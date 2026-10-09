package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.data.source.HhkanSource
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class DoubanRefreshPlayReferenceTest {
    @Test
    fun `refresh maps a changed line id to the selected source and same named episode`() {
        val previous = detail(
            line("高清线路", "old-content", "第1集", "第2集"),
            line("备用线路", "backup-content", "第1集", "第2集"),
        )
        val refreshed = detail(
            line("备用线路", "backup-new", "第1集", "第2集"),
            line("高清线路", "fresh-content", "第2集", "第1集"),
        )

        val target = DoubanRefreshPlayReference.map(
            selectedLineId = "old-content:0",
            selectedLineName = "高清线路",
            selectedEpisode = "1",
            previous = previous,
            refreshed = refreshed,
        )

        assertEquals(DoubanRefreshPlayReference.Target("fresh-content:0", "0"), target)
    }

    @Test
    fun `repeated refreshes keep mapping from the detail snapshot behind the unchanged UI reference`() {
        val uiDetail = detail(line("高清线路", "original-content", "第1集", "第2集"))
        val uiLineId = "original-content:0"
        val firstRefresh = detail(line("高清线路", "first-content", "第2集", "第1集"))
        val secondRefresh = detail(line("高清线路", "second-content", "第3集", "第1集", "第2集"))

        val first = DoubanRefreshPlayReference.map(
            selectedLineId = uiLineId,
            selectedLineName = "高清线路",
            selectedEpisode = "0",
            previous = uiDetail,
            refreshed = firstRefresh,
        )
        val second = DoubanRefreshPlayReference.map(
            selectedLineId = uiLineId,
            selectedLineName = "高清线路",
            selectedEpisode = "0",
            previous = uiDetail,
            refreshed = secondRefresh,
        )

        assertEquals(DoubanRefreshPlayReference.Target("first-content:0", "1"), first)
        assertEquals(DoubanRefreshPlayReference.Target("second-content:0", "1"), second)
    }

    @Test
    fun `refresh refuses to silently switch when selected source is gone`() {
        val previous = detail(line("高清线路", "old-content", "第1集"))
        val refreshed = detail(line("其他线路", "other-content", "第1集"))

        val target = DoubanRefreshPlayReference.map(
            selectedLineId = "old-content:0",
            selectedLineName = "高清线路",
            selectedEpisode = "0",
            previous = previous,
            refreshed = refreshed,
        )

        assertNull(target)
    }

    @Test
    fun `last good preference reorders but keeps all locally discovered sources`() {
        val local = listOf("source-a:content:0", "source-b:content:0", "source-c:content:0")
            .mapIndexed { index, id ->
                LineInfo("vod", "线路${index + 1}", "", "detail|$id|1")
            }

        val reordered = PreferredPlayableLines.reorder(local, "source-b:content:0", "detail")
        assertEquals(listOf("source-b:content:0", "source-a:content:0", "source-c:content:0"),
            reordered.map { it.playRef.split('|')[1] })

        val rememberedOnly = PreferredPlayableLines.reorder(local, "source-z:content:0", "detail")
        assertEquals("source-z:content:0", rememberedOnly.first().playRef.split('|')[1])
        assertEquals(local.map { it.playRef }, rememberedOnly.drop(1).map { it.playRef })

        val rememberedEpisode = PreferredPlayableLines.reorder(
            local,
            "source-z:content:0",
            "detail",
            epIndex = 7,
        )
        assertEquals("detail|source-z:content:0|7", rememberedEpisode.first().playRef)
    }

    private fun line(name: String, id: String, vararg episodeNames: String) =
        HhkanSource.Companion.LineDetail(
            name,
            episodeNames.mapIndexed { index, episodeName ->
                Triple(episodeName, "$id:0", index.toString())
            },
        )

    private fun detail(vararg lines: HhkanSource.Companion.LineDetail) =
        HhkanSource.Companion.DetailInfo(
            title = "",
            year = "",
            desc = "",
            poster = "",
            rating = 0.0,
            lines = lines.toList(),
        )
}

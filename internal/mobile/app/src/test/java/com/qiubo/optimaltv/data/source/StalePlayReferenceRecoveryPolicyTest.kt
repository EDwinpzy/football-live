package com.qiubo.optimaltv.data.source

import kotlinx.coroutines.CancellationException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

class StalePlayReferenceRecoveryPolicyTest {
    @Test
    fun `first unavailable Douban reference refreshes once and retries the same source and episode`() {
        val previous = detail(line("线路甲", "old-source:content", "第1集", "第2集"))
        val refreshed = detail(line("线路甲", "new-source:content", "第2集", "番外", "第1集"))
        val requests = mutableListOf<String>()

        val result = StalePlayReferenceRecoveryPolicy.resolve(
            isDouban = true,
            forceRefresh = false,
        ) { refresh ->
            if (!refresh) {
                requests += "play:old-source:content/1"
                throw VodPlayReferenceUnavailableException()
            }
            requests += "detail:refresh"
            val target = FreshPlayReferencePolicy.remapDouban(
                previous = previous,
                refreshed = refreshed,
                sourceLabel = "线路甲",
                oldLineId = "old-source:content",
                oldEpisode = "1",
            ) ?: error("selected source/episode should map")
            requests += "play:${target.lineId}/${target.episode}"
            "https://cdn.example/same-episode.m3u8"
        }

        assertEquals("https://cdn.example/same-episode.m3u8", result)
        assertEquals(
            listOf("play:old-source:content/1", "detail:refresh", "play:new-source:content/0"),
            requests,
        )
    }

    @Test
    fun `second unavailable reference is returned without a third request`() {
        var calls = 0
        val failure = runCatching {
            StalePlayReferenceRecoveryPolicy.resolve(isDouban = true, forceRefresh = false) {
                calls++
                throw VodPlayReferenceUnavailableException()
            }
        }.exceptionOrNull()

        assertTrue(failure is VodPlayReferenceUnavailableException)
        assertEquals(2, calls)
    }

    @Test
    fun `forced refresh and other source types do not trigger another refresh`() {
        val stale = VodPlayReferenceUnavailableException()
        for ((isDouban, forceRefresh) in listOf(true to true, false to false)) {
            var calls = 0
            val failure = runCatching {
                StalePlayReferenceRecoveryPolicy.resolve(isDouban, forceRefresh) {
                    calls++
                    throw stale
                }
            }.exceptionOrNull()
            assertSame(stale, failure)
            assertEquals(1, calls)
        }
    }

    @Test
    fun `cancellation is propagated without starting recovery`() {
        val cancelled = CancellationException("cancel playback")
        var calls = 0
        val failure = runCatching {
            StalePlayReferenceRecoveryPolicy.resolve(isDouban = true, forceRefresh = false) {
                calls++
                throw cancelled
            }
        }.exceptionOrNull()

        assertSame(cancelled, failure)
        assertEquals(1, calls)
    }

    private fun line(name: String, id: String, vararg episodes: String) =
        HhkanSource.Companion.LineDetail(
            name,
            episodes.mapIndexed { index, episode -> Triple(episode, id, index.toString()) },
        )

    private fun detail(vararg lines: HhkanSource.Companion.LineDetail) =
        HhkanSource.Companion.DetailInfo(
            title = "影片", year = "2026", desc = "", poster = "", rating = 0.0,
            lines = lines.toList(),
        )
}

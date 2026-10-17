package com.qiubo.optimaltv.data.repo

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class LiveStreamPayloadParserTest {
    @Test
    fun `player request supports cached opens and encodes dynamic source ids`() {
        assertEquals(
            "/api/stream/127583?src=all&fresh=1",
            LiveStreamRequestPath.build("127583", "all", fresh = true),
        )
        assertEquals(
            "/api/stream/127583?src=all",
            LiveStreamRequestPath.build("127583", "all", fresh = false),
        )
        assertEquals(
            "/api/stream/127583?src=channel%3Aq2+-fr&fresh=1",
            LiveStreamRequestPath.build("127583", "channel:q2 -fr", fresh = true),
        )
    }

    @Test
    fun `all returned playable dynamic lines keep server order labels and referers`() {
        val root = JSONObject(
            """
            {
              "sources": [
                {"url":"https://cdn.example/live-a.m3u8","player":"m3u8","source":"88z_27-en","name":"Sky Sports - EN","referer":"https://one.example/embed"},
                {"url":"https://cdn.example/live-b.m3u8","player":"m3u8","source":"channel:q2","name":"法甲 - FR","referer":"https://two.example/embed"},
                {"url":"https://cdn.example/live-c.m3u8","player":"jwplayer","source":"embed-only","name":"Embed only","referer":"https://three.example/embed"},
                {"url":"","player":"m3u8","source":"missing-url","name":"Missing URL"},
                {"url":"https://cdn.example/live-a2.m3u8","player":"m3u8","source":"88z_27-en","name":"duplicate"}
              ]
            }
            """.trimIndent(),
        )

        val actual = LiveStreamPayloadParser.available(root)

        assertEquals(listOf("88z_27-en", "channel:q2"), actual.map { it.source })
        assertEquals(listOf("Sky Sports - EN", "法甲 - FR"), actual.map { it.name })
        assertEquals(
            listOf("https://one.example/embed", "https://two.example/embed"),
            actual.map { it.referer },
        )
    }

    @Test
    fun `dynamic line with no server label falls back to its own source id`() {
        val parsed = LiveStreamPayloadParser.available(
            JSONObject(
                """{"sources":[{"url":"https://cdn.example/live.m3u8","player":"m3u8","source":"line-9"}]}""",
            ),
        )

        assertEquals(1, parsed.size)
        assertEquals("line-9", parsed.single().name)
    }

    @Test
    fun `renewal keeps requested source and falls back to that lines referer`() {
        val renewed = LiveStreamPayloadParser.renewed(
            root = JSONObject("""{"url":"https://cdn.example/renewed.m3u8","source":"channel:q2"}"""),
            requestedSource = "channel:q2",
            fallbackName = "法甲 - FR",
            fallbackReferer = "https://two.example/embed",
        )

        assertEquals("channel:q2", renewed?.source)
        assertEquals("法甲 - FR", renewed?.name)
        assertEquals("https://two.example/embed", renewed?.referer)
        assertEquals("https://cdn.example/renewed.m3u8", renewed?.url)
    }

    @Test
    fun `renewal rejects a mismatched source id`() {
        val renewed = LiveStreamPayloadParser.renewed(
            root = JSONObject("""{"url":"https://cdn.example/renewed.m3u8","source":"other-line"}"""),
            requestedSource = "channel:q2",
        )

        assertNull(renewed)
    }

    @Test
    fun `relay carries a dynamic source referer`() {
        val relay = LiveRepository.relayUrl(
            "https://api.example.test",
            "https://cdn.example.test/live.m3u8?token=x",
            "https://taifood-blog.asia/embed/match",
        )

        assertTrue(relay.contains("u=https%3A%2F%2Fcdn.example.test%2Flive.m3u8%3Ftoken%3Dx"))
        assertTrue(relay.contains("ref=https%3A%2F%2Ftaifood-blog.asia%2Fembed%2Fmatch"))
        assertTrue(!relay.contains("football_vpn"))
    }

    @Test
    fun `relay defaults to direct routing`() {
        val relay = LiveRepository.relayUrl("https://api.example.test", "https://cdn.example.test/live.m3u8")

        assertTrue(!relay.contains("football_vpn"))
    }

    @Test
    fun `stale source list from the backend stays playable`() {
        // 后端在上游限流/超时时会回退上一次成功清单并标 stale:true（2026-10-17）。
        // App 必须照常当作可用信号源，不能因为多了这个标记就当成空列表。
        val parsed = LiveStreamPayloadParser.available(
            JSONObject(
                """
                {
                  "stale": true,
                  "cachedAt": 1760000000,
                  "url": "https://cdn.example/live-a.m3u8",
                  "sources": [
                    {"url":"https://cdn.example/live-a.m3u8","player":"m3u8","source":"88z_4","name":"线路A2","referer":"https://www.88zhibo.tv/"}
                  ]
                }
                """.trimIndent(),
            ),
        )

        assertEquals(listOf("88z_4"), parsed.map { it.source })
        assertEquals(listOf("线路A2"), parsed.map { it.name })
    }
}

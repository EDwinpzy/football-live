package com.qiubo.optimaltv.data.source

import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class HhkanVodRoutingTest {
    private lateinit var server: ServerSocket
    private lateinit var origin: String
    private val running = AtomicBoolean(false)
    private val responses = ConcurrentHashMap<String, String>()
    private val responseSequences = ConcurrentHashMap<String, ConcurrentLinkedQueue<String>>()
    private val requests = ConcurrentHashMap<String, Int>()
    private val requestOrder = CopyOnWriteArrayList<String>()
    private var acceptThread: Thread? = null

    @Before
    fun startServer() {
        server = ServerSocket(0, 10, java.net.InetAddress.getByName("127.0.0.1"))
        origin = "http://127.0.0.1:${server.localPort}"
        running.set(true)
        acceptThread = thread(isDaemon = true) {
            while (running.get()) {
                val client = runCatching { server.accept() }.getOrNull() ?: break
                thread(isDaemon = true) { serve(client) }
            }
        }
    }

    @After
    fun stopServer() {
        running.set(false)
        if (::server.isInitialized) runCatching { server.close() }
        acceptThread?.join(250)
    }

    private fun serve(client: Socket) {
        client.use {
            val input = it.getInputStream().bufferedReader(Charsets.US_ASCII)
            val path = input.readLine().orEmpty().split(' ').getOrNull(1).orEmpty()
            while (input.readLine()?.isNotEmpty() == true) Unit
            requests.merge(path, 1, Int::plus)
            requestOrder += path
            val body = (responseSequences[path]?.poll() ?: responses[path] ?: "{}").toByteArray(Charsets.UTF_8)
            val output = it.getOutputStream()
            output.write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray(Charsets.US_ASCII))
            output.write(body)
            output.flush()
        }
    }

    @Test
    fun hhkanCatalogItemsKeepTheSourcePrefixInTheirDetailReference() {
        responses["/hhkan/latest?page=1"] = """{"items":[{"id":123,"title":"首页推荐"}]}"""

        val item = HhkanSource.fetchLatest(origin, okhttp3.OkHttpClient()).single()

        assertEquals("hhkan:123", item.id)
        assertEquals("hhkan:123", item.detailRef)
        assertEquals(1, requests["/hhkan/latest?page=1"])
    }

    @Test
    fun hhkanDetailReferencesUseNativeDetailRouteAndKeepEpisodeIds() {
        responses["/hhkan/detail/123"] = """{"title":"剧名","year":"2026","desc":"简介","sources":[{"name":"线路甲","episodes":[{"ep":"第1集","pid":45,"vid":6}]}]}"""

        val detail = VodApiSource.fetchDetail(origin, okhttp3.OkHttpClient(), "hhkan:123")

        assertEquals("剧名", detail.title)
        assertEquals(listOf(Triple("第1集", "45", "6")), detail.lines.single().episodes)
        assertEquals(1, requests["/hhkan/detail/123"])
    }

    @Test
    fun hhkanPlaybackReferencesUseNativeRouteWithVodPidAndVid() {
        responses["/hhkan/play/123/45/6"] = """{"sources":[{"url":"https://cdn.example/video.m3u8"}]}"""

        val urls = VodApiSource.fetchPlay(origin, okhttp3.OkHttpClient(), "hhkan:123", "45", "6")

        assertEquals(listOf("https://cdn.example/video.m3u8"), urls)
        assertEquals(1, requests["/hhkan/play/123/45/6"])
    }

    @Test
    fun hhkanManualRetryUsesFreshPlayRoute() {
        responses["/hhkan/play/123/45/6?fresh=1"] = """{"sources":[{"url":"https://cdn.example/refreshed.m3u8"}]}"""

        val urls = VodApiSource.fetchPlay(
            origin, okhttp3.OkHttpClient(), "hhkan:123", "45", "6", fresh = true,
        )

        assertEquals(listOf("https://cdn.example/refreshed.m3u8"), urls)
        assertEquals(listOf("/hhkan/play/123/45/6?fresh=1"), requestOrder.toList())
    }

    @Test
    fun doubanDetailAndPlaybackReferencesKeepUsingVodRoutes() {
        responses["/vod/detail/456"] = """{"title":"豆瓣影片","sources":[]}"""
        responses["/vod/play/456/douban-source%3Acontent/0"] = """{"url":"https://cdn.example/douban.m3u8"}"""

        val detail = VodApiSource.fetchDetail(origin, okhttp3.OkHttpClient(), "douban:456")
        val urls = VodApiSource.fetchPlay(origin, okhttp3.OkHttpClient(), "douban:456", "douban-source:content", "0")

        assertEquals("豆瓣影片", detail.title)
        assertEquals(listOf("https://cdn.example/douban.m3u8"), urls)
        assertEquals(1, requests["/vod/detail/456"])
        assertEquals(1, requests["/vod/play/456/douban-source%3Acontent/0"])
    }

    @Test
    fun doubanManualRetryRemapsChangedLineIdsAndKeepsTheSelectedEpisode() {
        responses["/vod/detail/456?refresh=1"] = """{"title":"豆瓣影片","sources":[{"name":"线路甲","id":"fresh-source:content","episodes":[{"name":"第1集"},{"name":"番外"},{"name":"第2集"}]}]}"""
        responses["/vod/play/456/fresh-source%3Acontent/2"] = """{"url":"https://cdn.example/refreshed-douban.m3u8"}"""
        val previousDetail = HhkanSource.Companion.DetailInfo(
            title = "豆瓣影片",
            year = "2026",
            desc = "",
            poster = "",
            rating = 0.0,
            lines = listOf(
                HhkanSource.Companion.LineDetail(
                    "线路甲",
                    listOf(
                        Triple("第1集", "old-source:content", "0"),
                        Triple("第2集", "old-source:content", "1"),
                    ),
                ),
            ),
        )

        val result = VodApiSource.fetchPlayResult(
            origin,
            okhttp3.OkHttpClient(),
            "douban:456",
            "old-source:content",
            "1",
            fresh = true,
            sourceLabel = "线路甲",
            previousDetail = previousDetail,
        )

        assertEquals(listOf("https://cdn.example/refreshed-douban.m3u8"), result.urls)
        assertEquals("fresh-source:content", result.lineId)
        assertEquals("2", result.episode)
        assertFalse(result.selectionUnavailable)
        assertEquals(
            listOf("/vod/detail/456?refresh=1", "/vod/play/456/fresh-source%3Acontent/2"),
            requestOrder.toList(),
        )
    }

    @Test
    fun doubanRepeatedManualRetriesMapFromTheUnchangedUiSnapshot() {
        val refreshPath = "/vod/detail/456?refresh=1"
        responseSequences[refreshPath] = ConcurrentLinkedQueue(
            listOf(
                """{"title":"豆瓣影片","sources":[{"name":"线路乙","id":"backup:content","episodes":[{"name":"第1集"},{"name":"第2集"}]},{"name":"线路甲","id":"fresh-one:content","episodes":[{"name":"第2集"},{"name":"番外"},{"name":"第1集"}]}]}""",
                """{"title":"豆瓣影片","sources":[{"name":"线路甲","id":"fresh-two:content","episodes":[{"name":"第1集"},{"name":"番外"},{"name":"第2集"}]},{"name":"线路乙","id":"backup-two:content","episodes":[{"name":"第1集"},{"name":"第2集"}]}]}""",
            ).toCollection(ConcurrentLinkedQueue()),
        )
        responses["/vod/play/456/fresh-one%3Acontent/0"] = """{"url":"https://cdn.example/retry-one.m3u8"}"""
        responses["/vod/play/456/fresh-two%3Acontent/2"] = """{"url":"https://cdn.example/retry-two.m3u8"}"""
        val unchangedUiSnapshot = HhkanSource.Companion.DetailInfo(
            title = "豆瓣影片",
            year = "2026",
            desc = "",
            poster = "",
            rating = 0.0,
            lines = listOf(
                HhkanSource.Companion.LineDetail(
                    "线路甲",
                    listOf(
                        Triple("第1集", "old-source:content", "0"),
                        Triple("第2集", "old-source:content", "1"),
                    ),
                ),
                HhkanSource.Companion.LineDetail(
                    "线路乙",
                    listOf(
                        Triple("第1集", "backup:content", "0"),
                        Triple("第2集", "backup:content", "1"),
                    ),
                ),
            ),
        )

        fun freshRetry() = VodApiSource.fetchPlayResult(
            base = origin,
            client = okhttp3.OkHttpClient(),
            id = "douban:456",
            lineId = "old-source:content",
            episode = "1",
            fresh = true,
            sourceLabel = "线路甲",
            // Deliberately pass the same detail snapshot held by the unchanged UI on both retries.
            previousDetail = unchangedUiSnapshot,
        )

        val first = freshRetry()
        val second = freshRetry()

        assertEquals(listOf("https://cdn.example/retry-one.m3u8"), first.urls)
        assertEquals("fresh-one:content", first.lineId)
        assertEquals("0", first.episode)
        assertEquals(listOf("https://cdn.example/retry-two.m3u8"), second.urls)
        assertEquals("fresh-two:content", second.lineId)
        assertEquals("2", second.episode)
        assertEquals(
            listOf(
                refreshPath,
                "/vod/play/456/fresh-one%3Acontent/0",
                refreshPath,
                "/vod/play/456/fresh-two%3Acontent/2",
            ),
            requestOrder.toList(),
        )
    }

    @Test
    fun doubanFreshRetryDoesNotFallBackToStaleOrAnotherSourceWhenSelectionDisappears() {
        responses["/vod/detail/456?refresh=1"] = """{"title":"豆瓣影片","sources":[{"name":"线路乙","id":"other-source:content","episodes":[{"name":"第1集"},{"name":"第2集"}]}]}"""
        responses["/vod/play/456/old-source%3Acontent/1"] = """{"url":"https://cdn.example/stale.m3u8"}"""
        val previousDetail = HhkanSource.Companion.DetailInfo(
            title = "豆瓣影片",
            year = "2026",
            desc = "",
            poster = "",
            rating = 0.0,
            lines = listOf(
                HhkanSource.Companion.LineDetail(
                    "线路甲",
                    listOf(
                        Triple("第1集", "old-source:content", "0"),
                        Triple("第2集", "old-source:content", "1"),
                    ),
                ),
            ),
        )

        val result = VodApiSource.fetchPlayResult(
            origin,
            okhttp3.OkHttpClient(),
            "douban:456",
            "old-source:content",
            "1",
            fresh = true,
            sourceLabel = "线路甲",
            previousDetail = previousDetail,
        )

        assertEquals(emptyList<String>(), result.urls)
        assertEquals(true, result.selectionUnavailable)
        assertEquals(listOf("/vod/detail/456?refresh=1"), requestOrder.toList())
    }
}

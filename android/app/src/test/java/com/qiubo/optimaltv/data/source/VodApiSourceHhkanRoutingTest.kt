package com.qiubo.optimaltv.data.source

import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class VodApiSourceHhkanRoutingTest {
    @Test
    fun `好好看首页条目保留明确的 HHKan detailRef 前缀`() {
        withJsonServer(
            """{"sections":[{"title":"最近更新","items":[{"id":721,"title":"测试剧"}]}]}""",
        ) { base, requestedPath ->
            val home = HhkanSource.fetchHome(base, OkHttpClient())

            assertEquals("/hhkan/home", requestedPath.get())
            assertEquals("hhkan:721", home.sections.single().items.single().detailRef)
        }
    }

    @Test
    fun `带 HHKan 引用的详情请求走原版路由并保留 pid 与 vid`() {
        withJsonServer(
            """{"title":"测试剧","year":"2025","sources":[{"name":"线路A","episodes":[{"ep":"第1集","pid":23,"vid":45}]}]}""",
        ) { base, requestedPath ->
            val detail = VodApiSource.fetchDetail(base, OkHttpClient(), "hhkan:721")

            assertEquals("/hhkan/detail/721", requestedPath.get())
            assertEquals(Triple("第1集", "23", "45"), detail.lines.single().episodes.single())
        }
    }

    @Test
    fun `刷新 HHKan 详情绕过缓存并补充线路`() {
        withJsonServer("""{"title":"测试剧","sources":[]}""") { base, requestedPath ->
            VodApiSource.fetchDetail(base, OkHttpClient(), "hhkan:721", refresh = true)

            assertEquals("/hhkan/detail/721?refresh=1&enrich=1", requestedPath.get())
        }
    }

    @Test
    fun `延迟片源的 HHKan 详情仍可强制绕过缓存`() {
        withJsonServer("""{"title":"测试剧","sources":[]}""") { base, requestedPath ->
            VodApiSource.fetchDetail(
                base,
                OkHttpClient(),
                "hhkan:721",
                deferSources = true,
                refresh = true,
            )

            assertEquals("/hhkan/detail/721?refresh=1", requestedPath.get())
        }
    }

    @Test
    fun `带 HHKan 引用的播放请求传递 vodid pid vid 三段`() {
        withJsonServer(
            """{"sources":[{"url":"https://media.example/episode.m3u8"}]}""",
        ) { base, requestedPath ->
            val urls = VodApiSource.fetchPlay(base, OkHttpClient(), "hhkan:721", "23", "45")

            assertEquals("/hhkan/play/721/23/45", requestedPath.get())
            assertEquals(listOf("https://media.example/episode.m3u8"), urls)
        }
    }

    @Test
    fun `手动重试 HHKan 播放请求绕过服务端缓存`() {
        withJsonServer(
            """{"sources":[{"url":"https://media.example/fresh.m3u8"}]}""",
        ) { base, requestedPath ->
            val urls = VodApiSource.fetchPlay(base, OkHttpClient(), "hhkan:721", "23", "45", fresh = true)

            assertEquals("/hhkan/play/721/23/45?fresh=1", requestedPath.get())
            assertEquals(listOf("https://media.example/fresh.m3u8"), urls)
        }
    }

    @Test
    fun `豆瓣播放重试保留既有播放路由`() {
        withJsonServer(
            """{"url":"https://media.example/douban.m3u8"}""",
        ) { base, requestedPath ->
            val urls = VodApiSource.fetchPlay(
                base, OkHttpClient(), "douban:721", "source:content", "3", fresh = true,
            )

            assertEquals("/vod/play/721/source%3Acontent/3", requestedPath.get())
            assertEquals(listOf("https://media.example/douban.m3u8"), urls)
        }
    }

    @Test
    fun `豆瓣手动重试先可强制刷新详情`() {
        withJsonServer(
            """{"title":"测试剧","sources":[]}""",
        ) { base, requestedPath ->
            VodApiSource.fetchDetail(base, OkHttpClient(), "douban:721", refresh = true)

            assertEquals("/vod/detail/721?refresh=1", requestedPath.get())
        }
    }

    @Test
    fun `只有明确的400播放线路不可用响应会标记为陈旧引用`() {
        withJsonServer(
            """{"error":"播放线路不可用"}""",
            statusCode = 400,
        ) { base, requestedPath ->
            val failure = runCatching {
                VodApiSource.fetchPlay(base, OkHttpClient(), "douban:721", "old-source:content", "0")
            }.exceptionOrNull()

            assertEquals("/vod/play/721/old-source%3Acontent/0", requestedPath.get())
            org.junit.Assert.assertTrue(failure is VodPlayReferenceUnavailableException)
        }
    }

    @Test
    fun `其他HTTP错误不会被标记为陈旧引用`() {
        withJsonServer(
            """{"error":"播放线路不可用"}""",
            statusCode = 500,
        ) { base, _ ->
            val failure = runCatching {
                VodApiSource.fetchPlay(base, OkHttpClient(), "douban:721", "old-source:content", "0")
            }.exceptionOrNull()

            org.junit.Assert.assertFalse(failure is VodPlayReferenceUnavailableException)
        }
    }

    private fun withJsonServer(
        responseBody: String,
        statusCode: Int = 200,
        block: (base: String, requestedPath: AtomicReference<String>) -> Unit,
    ) {
        val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        val requestedPath = AtomicReference("")
        val serverThread = thread(isDaemon = true) {
            runCatching {
                server.accept().use { socket -> serve(socket, responseBody, requestedPath, statusCode) }
            }
        }
        try {
            block("http://127.0.0.1:${server.localPort}", requestedPath)
        } finally {
            server.close()
            serverThread.join(1_000)
        }
    }

    private fun serve(socket: Socket, body: String, requestedPath: AtomicReference<String>, statusCode: Int) {
        val input = socket.getInputStream().bufferedReader(Charsets.US_ASCII)
        val requestLine = input.readLine().orEmpty()
        requestedPath.set(requestLine.split(' ').getOrNull(1).orEmpty())
        while (input.readLine()?.isNotEmpty() == true) Unit

        val bytes = body.toByteArray(Charsets.UTF_8)
        socket.getOutputStream().apply {
            val statusText = if (statusCode == 200) "OK" else "Bad Request"
            write("HTTP/1.1 $statusCode $statusText\r\n".toByteArray(Charsets.US_ASCII))
            write("Content-Type: application/json; charset=utf-8\r\n".toByteArray(Charsets.US_ASCII))
            write("Content-Length: ${bytes.size}\r\nConnection: close\r\n\r\n".toByteArray(Charsets.US_ASCII))
            write(bytes)
            flush()
        }
    }
}

package com.qiubo.optimaltv.data.repo

import okhttp3.Call
import okhttp3.Callback
import okhttp3.Dispatcher
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.IOException
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

/**
 * 「做直播找不到可用信号源」的设备侧排队回归（2026-10-17）。
 *
 * 现场形态：赛程/战报/集锦/新闻/信号源全部打到同一个 127.0.0.1:8090。生产请求走
 * [liveHttpResponse]（`call.enqueue`，**异步**），而 OkHttp 的 `maxRequestsPerHost`
 * （默认 5）只约束异步调用。旧版 `liveSourceHttp` 由主客户端 newBuilder 派生、沿用
 * 同一个 Dispatcher，进足球页几路并发就能占满 5 个名额。
 *
 * 关键实测结论（本测试锁住）：**排队期间 callTimeout 不生效**——OkHttp 在 call 真正
 * 开始执行时才进入超时窗口，所以被挡在 ready 队列里的信号源请求不会「45s 后超时报
 * 无信号」，而是**无限期挂着**：播放器停在「正在查找片源…」，用户彻底退出 app
 * （新进程、新队列）再进才恢复。
 *
 * 桩服务：前 5 条连接收下不回（占住名额），第 6 条立刻回 200。
 * ① 共用 Dispatcher：第 6 个请求 1.5s 内既无回调也未到达服务端（即使 callTimeout=800ms）；
 * ② 独立 Dispatcher：同样的第 6 个请求立刻到达并拿到 200。
 */
class LiveSourceDispatcherQueueTest {

    private lateinit var server: ServerSocket
    private val accepted = AtomicInteger(0)
    private val held = mutableListOf<Socket>()
    private val clients = mutableListOf<OkHttpClient>()

    @Volatile private var serving = true

    @Before
    fun setUp() {
        server = ServerSocket(0, 16, InetAddress.getByName("127.0.0.1"))
        Thread({
            while (serving) {
                val socket = try {
                    server.accept()
                } catch (_: IOException) {
                    return@Thread
                }
                val index = accepted.incrementAndGet()
                if (index <= SLOW_CONNECTIONS) {
                    synchronized(held) { held.add(socket) }   // 收下不回：占住 per-host 名额
                } else {
                    Thread({
                        runCatching {
                            socket.getOutputStream().write(
                                ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" +
                                    "Content-Length: 2\r\nConnection: close\r\n\r\n{}").toByteArray()
                            )
                            socket.getOutputStream().flush()
                        }
                        runCatching { socket.close() }
                    }, "stub-fast-$index").apply { isDaemon = true }.start()
                }
            }
        }, "stub-server").apply { isDaemon = true }.start()
    }

    @After
    fun tearDown() {
        serving = false
        runCatching { server.close() }
        synchronized(held) {
            held.forEach { runCatching { it.close() } }
            held.clear()
        }
        clients.forEach { runCatching { it.dispatcher.executorService.shutdownNow() } }
    }

    private fun url() = "http://127.0.0.1:${server.localPort}/api/stream/1?src=all"

    private fun clientWith(dispatcher: Dispatcher, callTimeoutMs: Long): OkHttpClient =
        OkHttpClient.Builder()
            .dispatcher(dispatcher)
            .connectTimeout(2, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .callTimeout(callTimeoutMs, TimeUnit.MILLISECONDS)
            .build()
            .also { clients.add(it) }

    private val ignoreCallback = object : Callback {
        override fun onFailure(call: Call, e: IOException) = Unit
        override fun onResponse(call: Call, response: Response) = response.close()
    }

    private fun enqueue(client: OkHttpClient, path: String) {
        client.newCall(Request.Builder().url("http://127.0.0.1:${server.localPort}$path").build())
            .enqueue(ignoreCallback)
    }

    private fun awaitAccepted(expected: Int) {
        val deadline = System.currentTimeMillis() + 5_000
        while (accepted.get() < expected && System.currentTimeMillis() < deadline) Thread.sleep(20)
        assertTrue("桩服务只收到 ${accepted.get()} 条连接，期望 $expected", accepted.get() >= expected)
    }

    /** 发一个信号源请求，最多等 [waitMs]；返回 (失败异常, HTTP 码, 是否已回调)。 */
    private fun probe(client: OkHttpClient, waitMs: Long): Triple<IOException?, Int?, Boolean> {
        val latch = CountDownLatch(1)
        val error = AtomicReference<IOException?>()
        val code = AtomicReference<Int?>()
        client.newCall(Request.Builder().url(url()).build()).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                error.set(e)
                latch.countDown()
            }

            override fun onResponse(call: Call, response: Response) {
                response.use { code.set(it.code) }
                latch.countDown()
            }
        })
        val done = latch.await(waitMs, TimeUnit.MILLISECONDS)
        return Triple(error.get(), code.get(), done)
    }

    @Test
    fun sharedDispatcherLeavesTheSixthRequestQueuedWithoutTimeout() {
        // 旧版形态：5 个慢接口 + 信号源请求共用同一个 Dispatcher
        val shared = Dispatcher().apply {
            maxRequests = 64
            maxRequestsPerHost = 5
        }
        val slow = clientWith(shared, callTimeoutMs = 0)   // 0 = 不限，模拟慢接口本身不超时
        repeat(SLOW_CONNECTIONS) { enqueue(slow, "/api/matches?fresh=1") }
        awaitAccepted(SLOW_CONNECTIONS)

        // callTimeout=800ms 也不生效：排队中的 call 还没开始计时
        val (error, code, done) = probe(clientWith(shared, callTimeoutMs = 800), waitMs = 1_500)

        assertFalse("排队中的请求不该有回调（拿到 $code / $error）", done)
        assertNull(error)
        assertNull(code)
        assertEquals(
            "信号源请求不该到达服务端（被 per-host 名额挡住）",
            SLOW_CONNECTIONS,
            accepted.get(),
        )
    }

    @Test
    fun dedicatedDispatcherReachesTheServerWhileSlowCallsAreStillQueued() {
        // 现版形态：慢接口用一个 Dispatcher，信号源请求用独立 Dispatcher
        val shared = Dispatcher().apply {
            maxRequests = 64
            maxRequestsPerHost = 5
        }
        val slow = clientWith(shared, callTimeoutMs = 0)
        repeat(SLOW_CONNECTIONS) { enqueue(slow, "/api/matches?fresh=1") }
        awaitAccepted(SLOW_CONNECTIONS)

        val dedicated = Dispatcher().apply {
            maxRequests = 24
            maxRequestsPerHost = 24
        }
        val (error, code, done) = probe(clientWith(dedicated, callTimeoutMs = 3_000), waitMs = 4_000)

        assertTrue("独立 Dispatcher 的请求应有回调", done)
        assertNull("独立 Dispatcher 不应超时：$error", error)
        assertEquals(200, code)
        assertEquals("独立 Dispatcher 的信号源请求应立刻到达", SLOW_CONNECTIONS + 1, accepted.get())
    }

    private companion object {
        /** OkHttp 默认 maxRequestsPerHost */
        const val SLOW_CONNECTIONS = 5
    }
}

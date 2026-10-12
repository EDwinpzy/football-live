package com.qiubo.optimaltv.playback

import android.annotation.SuppressLint
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.webkit.WebView
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URLDecoder
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import org.json.JSONObject

/**
 * 直播流解密桥（端口 127.0.0.1:8091，2026-08-29 需求 足球#1）：
 *
 * 后端 proxy.py 在安卓模式（ANDROID_MODE）下把播放器混淆页 POST 到
 * http://127.0.0.1:8091/decrypt，期望返回 {"url": "<m3u8 直链>"}。
 * 此前的 QuickJS 方案未落地，端口无人监听 → 直播全部「信号尚未开播/解析失败」。
 *
 * 实现：无头 WebView 是设备上唯一与源站浏览器同源的 JS 引擎——
 * 把播放器页以 wtmdjxkq.com 为 base 加载，先注入 document.write 缓冲钩子，
 * 轮询缓冲直到出现 m3u8.html?id=（或 .m3u8 直链），提取后按原 extract 语义返回。
 * 与 node 版 decrypt_stream.js 同语义：只读取脚本写出的内容，不跟随跳转。
 */
object StreamDecryptServer {
    private const val TAG = "OTV-Decrypt"
    private const val PORT = 8091
    private const val PLAYER_HOST = "http://wtmdjxkq.com/"

    /**
     * 解密池大小（2026-09-07 需求②）：旧版单 WebView 全局锁串行解密，每源 1.5~12s，
     * 大场次 10+ 源排队到分钟级才凑齐信号源选择条。无头 WebView 的耗时大头是等页面
     * JS 异步写出地址（postDelayed 轮询不占主线程），多实例并行是真并行；
     * 3 个兼顾吞吐与低端设备内存（~20-40MB/实例）。首实例照旧延迟 1.2s 预建，
     * 其余按需懒建（并发到来才补第二个/第三个）。
     */
    private const val POOL_SIZE = 3

    /** 注入在播放器脚本之前：接管 document.write/writeln，把写出的内容缓冲进 window.__otv */
    private const val HOOK_JS =
        "<script>window.__otv='';" +
            "(function(){var o=document.write.bind(document),ol=document.writeln.bind(document);" +
            "document.write=function(s){window.__otv+=String(s);};document.writeln=function(s){window.__otv+=String(s);};})();" +
            "</script>"

    @Volatile private var started = false
    private val mainHandler = Handler(Looper.getMainLooper())
    private val pool = arrayOfNulls<WebView>(POOL_SIZE)
    private var appContext: Context? = null
    @Volatile private var serverSocket: ServerSocket? = null
    @Volatile private var connPool: java.util.concurrent.ExecutorService? = null
    private val consecutiveTimeouts = java.util.concurrent.atomic.AtomicIntegerArray(POOL_SIZE)

    /** 解密并发闸：池里每个 WebView 一张票；解密段持票执行（替代旧全局 synchronized） */
    private val poolSem = java.util.concurrent.Semaphore(POOL_SIZE, true)

    fun ensureStarted(context: Context) {
        appContext = context.applicationContext
        if (started) return
        synchronized(this) {
            if (started) return
            started = true
            val appCtx = context.applicationContext
            // v1.8（2026-08-30）加固：accept 循环外层自愈——实测桥线程会因 socket 异常
            // 静默退出且无任何重启方（直播全部假死 70s/请求）；现在捕获后 1s 重听。
            // 连接处理挪到 4 线程池：单个慢连接（如读了半截头就断）不再卡死整个桥。
            val executor = java.util.concurrent.Executors.newFixedThreadPool(4) { r ->
                Thread(r, "otv-decrypt-conn").apply { isDaemon = true }
            }
            connPool = executor
            Thread {
                while (started) {
                    try {
                        ServerSocket(PORT, 8, InetAddress.getByName("127.0.0.1")).use { server ->
                            serverSocket = server
                            Log.i(TAG, "decrypt bridge listening :$PORT")
                            while (started) {
                                val sock = server.accept()
                                executor.execute {
                                    runCatching { handle(sock) }
                                        .onFailure {
                                            Log.w(TAG, "decrypt request failed: ${it.message}")
                                            runCatching { sock.close() }
                                        }
                                }
                            }
                        }
                        return@Thread
                    } catch (e: Exception) {
                        if (!started) return@Thread
                        Log.e(TAG, "decrypt bridge crashed (${e.message}) → 1s 后重启监听")
                        try { Thread.sleep(1_000) } catch (_: InterruptedException) { return@Thread }
                    } finally {
                        serverSocket = null
                    }
                }
            }.apply { isDaemon = true; name = "otv-decrypt" }.start()
            // 主线程预建首个无头 WebView（首次解密免等待）——v1.10 延迟 1.2s：
            // WebView 初始化上百毫秒，冷启动首帧不与它抢主线程；用户进直播页前通常已就绪。
            // v1.19 死锁修复：旧版在主线程调带 latch.await 的 ensureWebView——
            // await 等的是排在本线程队列后面的 post 任务，永远等不到 → 每次冷启动
            // 主线程白卡 5 秒（正落在启动门闸期间，转圈冻结的直接根因）。
            // 预建本就在主线程执行，直接创建即可，无需跨线程同步。
            mainHandler.postDelayed({
                runCatching { createWebViewLocked(appCtx, 0) }
            }, 1_200)

        }
    }

    /** 长时体检用（2026-09-16）：解密池已建实例数。0 = 换源/解析必失败（池被回收或
     *  渲染进程死亡），是「切信号源没反应」的现场指标。仅主线程读。 */
    fun readySlots(): Int = runCatching { pool.count { it != null } }.getOrDefault(-1)

    /** Rebuild every WebView slot while keeping the local bridge available. */
    fun reset() {
        Log.w(TAG, "decrypt bridge reset")
        mainHandler.post {
            for (slot in pool.indices) {
                rebuildSlot(slot)
                consecutiveTimeouts.set(slot, 0)
            }
        }
    }

    /** Explicit app exit: close the listener, workers and WebView renderer pool. */
    fun shutdown() {
        if (!started) return
        started = false
        runCatching { serverSocket?.close() }
        serverSocket = null
        connPool?.shutdownNow()
        connPool = null
        // This is a terminal process shutdown. reset() posts WebView rebuilds to the main thread,
        // which only adds work while the Activity is finishing and the process is about to exit.
        Log.i(TAG, "decrypt bridge shutdown")
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun ensureWebView(context: Context, slot: Int): WebView {
        pool[slot]?.let { return it }
        val latch = CountDownLatch(1)
        val ref = AtomicReference<WebView>()
        mainHandler.post {
            try {
                ref.set(createWebViewLocked(context, slot))
            } catch (e: Throwable) {
                Log.e(TAG, "webview create failed: ${e.message}")
            } finally {
                latch.countDown()
            }
        }
        latch.await(5, TimeUnit.SECONDS)
        return ref.get() ?: throw IllegalStateException("WebView 不可用")
    }

    /** 仅主线程调用：真正创建 WebView 并记入池（重复调用幂等；槽位越界时收拢到合法区间） */
    @SuppressLint("SetJavaScriptEnabled")
    private fun createWebViewLocked(context: Context, slot: Int): WebView {
        val s = slot.coerceIn(0, POOL_SIZE - 1)
        pool[s]?.let { return it }
        return WebView(context).apply {
            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                blockNetworkImage = true
                mediaPlaybackRequiresUserGesture = false
                mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            }
            layout(0, 0, 1280, 720)
            // ⑬（2026-09-08 需求⑬）解密池自愈：反复加载混淆播放器页（每场 10+ 源，
            // 每源一整页 JS）数小时后渲染进程必然内存崩溃；旧版无此回调，僵死 WebView
            // 永久留在池里——每次解密空等 12s 超时，三槽全僵 = 所有信号源解析失败
            // （「看半小时后所有信号源都失效」的设备端根因）。返回 true 自处理：
            // 主线程销毁并清槽，下个请求 ensureWebView 懒建新实例。
            webViewClient = object : android.webkit.WebViewClient() {
                override fun onRenderProcessGone(
                    view: WebView,
                    detail: android.webkit.RenderProcessGoneDetail,
                ): Boolean {
                    Log.e(TAG, "render gone slot=$s crashed=${detail.didCrash()} → 销毁重建")
                    // 回调在主线程：rebuildSlot 自带 destroy（view 即 pool[s]，幂等清理）
                    rebuildSlot(s)
                    return true
                }
            }
            pool[s] = this
        }
    }

    /** 仅主线程调用：销毁并清空槽位（渲染进程死亡/僵死自愈；进行中的解密会在
     * evaluateJavascript 抛 IllegalStateException → 已 runCatching 兜住按超时返回 null） */
    private fun rebuildSlot(slot: Int) {
        val wv = pool[slot] ?: return
        pool[slot] = null
        consecutiveTimeouts.set(slot, 0)
        runCatching { wv.destroy() }
    }

    /** 单连接处理：POST /decrypt body={"html","matchId"} → {"url":...}
     *  v1.8：头部行数设上限（畸形连接不会无限占线程）；解密段从池里取一个 WebView
     *  并发执行（旧版单实例串行是信号源补齐慢的根因之一） */
    private fun handle(sock: Socket) {
        sock.use { s ->
            s.soTimeout = 90_000
            val reader = BufferedReader(InputStreamReader(s.getInputStream(), Charsets.ISO_8859_1))
            var contentLength = 0
            var path = ""
            var headerLines = 0
            while (true) {
                val line = reader.readLine() ?: return
                if (line.isBlank()) break
                if (++headerLines > 100) return   // 垃圾数据流：直接断开
                if (line.startsWith("POST")) path = line.split(" ").getOrNull(1).orEmpty()
                if (line.startsWith("Content-Length", true)) {
                    contentLength = line.substringAfter(':').trim().toIntOrNull() ?: 0
                }
            }
            if (!path.startsWith("/decrypt")) {
                respond(s, """{"url":null,"error":"unknown path"}""")
                return
            }
            val body = CharArray(contentLength).let { buf ->
                var read = 0
                while (read < contentLength) {
                    val n = reader.read(buf, read, contentLength - read)
                    if (n < 0) break
                    read += n
                }
                String(buf, 0, read)
            }
            val json = runCatching { JSONObject(body) }.getOrElse {
                respond(s, """{"url":null,"error":"bad json"}""")
                return
            }
            val html = json.optString("html")
            val matchId = json.optString("matchId", "0")
            if (html.isBlank()) {
                respond(s, """{"url":null,"error":"empty html"}""")
                return
            }
            val url = runCatching { decryptPooled(html, matchId) }
                .onFailure { Log.w(TAG, "decrypt pooled failed: ${it.message}") }
                .getOrNull()
            Log.i(TAG, "decrypt match=$matchId → ${if (url != null) "OK" else "no url"}")
            respond(s, JSONObject().apply { put("url", url ?: JSONObject.NULL) }.toString())
        }
    }

    private fun respond(sock: Socket, body: String) {
        val bytes = body.toByteArray(Charsets.UTF_8)
        sock.getOutputStream().write(
            ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" +
                "Content-Length: ${bytes.size}\r\nConnection: close\r\n\r\n").toByteArray() + bytes,
        )
        sock.getOutputStream().flush()
    }

    /** 池化解密：阻塞取票 → 就近取空闲槽（懒建 WebView）→ 解密 → 还票 */
    private fun decryptPooled(html: String, matchId: String): String? {
        val ctx = appContext ?: return null
        poolSem.acquire()
        try {
            val slot = freeSlot()
            if (slot < 0) return null
            try {
                return decrypt(ctx, ensureWebView(ctx, slot), html, matchId, slot)
            } finally {
                freeSlot(slot)
            }
        } finally {
            poolSem.release()
        }
    }

    private val slotLock = Object()
    private val slotBusy = BooleanArray(POOL_SIZE)

    private fun freeSlot(): Int {
        synchronized(slotLock) {
            for (i in 0 until POOL_SIZE) {
                if (!slotBusy[i]) {
                    slotBusy[i] = true
                    return i
                }
            }
        }
        return -1
    }

    private fun freeSlot(i: Int) {
        synchronized(slotLock) { slotBusy[i] = false }
    }

    /** 加载播放器页并轮询写缓冲，提取流地址；超时返回 null（后端报「未开播」语义）。
     *  ⑬（2026-09-08）：零回调检测——12s 内 evaluateJavascript 一次回调都没有
     *  （正常未开播页也会秒回空串）说明该 WebView 已僵死（渲染进程死亡但回调未触发的
     *  形态，如 GPU 进程崩溃），返回 null 的同时请求主线程重建该槽，不留给下次再超时。 */
    private fun decrypt(context: Context, wv: WebView, html: String, matchId: String, slot: Int): String? {
        val latch = CountDownLatch(1)
        val result = AtomicReference<String?>(null)
        val callbacks = java.util.concurrent.atomic.AtomicInteger(0)
        var polls = 0
        fun poll() {
            polls++
            mainHandler.post {
                // 槽位 WebView 可能在本次解密进行中被 rebuildSlot 销毁（渲染进程
                // 崩溃自愈）——evaluateJavascript 会抛 IllegalStateException，按超时返回
                runCatching {
                    wv.evaluateJavascript("window.__otv||''") { quoted ->
                        callbacks.incrementAndGet()
                        // evaluateJavascript 返回值是 JSON 字符串字面量，反转义为原始文本
                        val raw = runCatching {
                            org.json.JSONTokener(quoted.trim()).nextValue().toString()
                        }.getOrDefault("")
                        extractUrl(raw)?.let {
                            result.set(it)
                            latch.countDown()
                            return@evaluateJavascript
                        }
                        if (polls < 30) mainHandler.postDelayed({ poll() }, 300)
                        else latch.countDown()
                    }
                }.onFailure {
                    Log.w(TAG, "poll failed slot=$slot: ${it.message}")
                    latch.countDown()
                }
            }
        }
        mainHandler.post {
            try {
                // base 带上播放器路径+id：与源站 iframe 环境一致（location.href 含 matchId）
                wv.loadDataWithBaseURL(
                    PLAYER_HOST + "ballbar.php?id=" + matchId,
                    HOOK_JS + html,
                    "text/html",
                    "utf-8",
                    null,
                )
                mainHandler.postDelayed({ poll() }, 400)
            } catch (e: Throwable) {
                Log.w(TAG, "load failed: ${e.message}")
                latch.countDown()
            }
        }
        latch.await(12, TimeUnit.SECONDS)
        val found = result.get()
        if (found != null) {
            consecutiveTimeouts.set(slot, 0)
        } else if (callbacks.get() == 0 && polls > 0) {
            Log.e(TAG, "slot=$slot 疑似僵死（零回调）→ 请求重建")
            mainHandler.post { rebuildSlot(slot) }
        } else if (consecutiveTimeouts.incrementAndGet(slot) >= 2) {
            Log.e(TAG, "slot=$slot 连续两次未解出地址 → 请求重建")
            mainHandler.post { rebuildSlot(slot) }
        }
        return found
    }

    /** 与 node/QuickJS 版 extractStreamUrl 同语义：m3u8.html?id= 优先，.m3u8 直链兜底 */
    private fun extractUrl(written: String): String? {
        val fm = Regex("m3u8\\.html\\?id=([^\"'\\s<]*)").find(written)
        if (fm != null) {
            val url = runCatching { URLDecoder.decode(fm.groupValues[1], "UTF-8") }
                .getOrElse { fm.groupValues[1] }
            if (url.startsWith("http://") || url.startsWith("https://")) return url
        }
        val dm = Regex("https?://[^\\s\"'<>\\\\]+\\.m3u8[^\\s\"'<>\\\\]*", RegexOption.IGNORE_CASE)
            .find(written)
        return dm?.value
    }
}

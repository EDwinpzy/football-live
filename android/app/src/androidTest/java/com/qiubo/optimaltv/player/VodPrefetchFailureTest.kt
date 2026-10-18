package com.qiubo.optimaltv.player

import androidx.room.Room
import androidx.lifecycle.ViewModelStore
import androidx.test.platform.app.InstrumentationRegistry
import com.qiubo.optimaltv.data.db.AppDb
import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.data.repo.VodRepository
import com.qiubo.optimaltv.data.repo.SelectedSourceRefreshException
import com.qiubo.optimaltv.ui.player.PlayerUiState
import com.qiubo.optimaltv.ui.player.PlayerViewModel
import com.qiubo.optimaltv.playback.EnginePlayState
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.assertFalse
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/** Real root VM coroutine + HTTP stale-reference refresh; no internet or real media required. */
class VodPrefetchFailureTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private lateinit var server: ServerSocket
    private lateinit var db: AppDb
    private lateinit var settings: SettingsStore
    private lateinit var originalBase: String
    private lateinit var vm: PlayerViewModel
    private lateinit var repo: VodRepository
    private val store = ViewModelStore()
    private val deadRefreshed = CountDownLatch(1)
    private val nextResolved = CountDownLatch(1)
    @Volatile private var serving = true

    @Before fun setUp() {
        server = ServerSocket(0, 10, InetAddress.getByName("127.0.0.1"))
        thread(isDaemon = true, name = "prefetch-fixture") {
            while (serving) {
                val socket = runCatching { server.accept() }.getOrNull() ?: break
                socket.use {
                    val reader = it.getInputStream().bufferedReader()
                    val path = reader.readLine().orEmpty().split(' ').getOrNull(1).orEmpty()
                    while (!reader.readLine().isNullOrEmpty()) { /* headers */ }
                    val (status, body) = when {
                        path.startsWith("/vod/detail/") -> {
                            deadRefreshed.countDown()
                            200 to """{"title":"fixture","sources":[]}"""
                        }
                        path.contains("good-line") -> {
                            nextResolved.countDown()
                            200 to """{"url":"https://example.com/fixture.m3u8"}"""
                        }
                        else -> 400 to """{"error":"播放线路不可用"}"""
                    }
                    val bytes = body.toByteArray(Charsets.UTF_8)
                    val header = "HTTP/1.1 $status Fixture\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n"
                    it.getOutputStream().write(header.toByteArray() + bytes)
                }
            }
        }
        db = Room.inMemoryDatabaseBuilder(context, AppDb::class.java).build()
        settings = SettingsStore(context)
        runBlocking {
            originalBase = settings.current().hhkanBaseUrl
            settings.setHhkanBase("http://127.0.0.1:${server.localPort}")
        }
        repo = VodRepository(settings, OkHttpClient(), context)
        instrumentation.runOnMainSync {
            vm = PlayerViewModel("douban:fixture", 0, repo, db, settings, autoBoot = false)
            store.put("player", vm)
            @Suppress("UNCHECKED_CAST")
            val state = PlayerViewModel::class.java.getDeclaredField("_ui").apply { isAccessible = true }
                .get(vm) as MutableStateFlow<PlayerUiState>
            state.value = vm.ui.value.copy(
                title = "current playable film", isLive = false, isPlaying = true,
                playState = EnginePlayState.READY, positionMs = 180_000L,
                lines = listOf(
                    LineInfo("douban:fixture", "current", "https://example.com/current.m3u8"),
                    LineInfo("douban:fixture", "unavailable alternate", "", "fixture|dead-line|0"),
                    LineInfo("douban:fixture", "next alternate", "", "fixture|good-line|0"),
                ),
            )
        }
    }

    @After fun tearDown() {
        instrumentation.runOnMainSync { store.clear() }
        runBlocking { settings.setHhkanBase(originalBase) }
        serving = false
        server.close()
        db.close()
    }

    @Test fun unavailableAlternateCannotCrashCurrentPlaybackAndNextLineStillPrefetches() {
        instrumentation.runOnMainSync {
            PlayerViewModel::class.java.getDeclaredMethod("prefetchLines").apply { isAccessible = true }.invoke(vm)
        }
        assertTrue("stale alternate must exercise the real refresh failure", deadRefreshed.await(12, TimeUnit.SECONDS))
        assertTrue("failure must not abort the remaining optional prefetches", nextResolved.await(12, TimeUnit.SECONDS))
        instrumentation.runOnMainSync {
            assertEquals(0, vm.ui.value.activeLine)
            assertEquals(180_000L, vm.ui.value.positionMs)
            assertEquals(EnginePlayState.READY, vm.ui.value.playState)
            assertTrue(vm.ui.value.isPlaying)
        }
    }

    @Test fun clearedSessionDoesNotContinuePrefetching() {
        instrumentation.runOnMainSync {
            PlayerViewModel::class.java.getDeclaredMethod("prefetchLines").apply { isAccessible = true }.invoke(vm)
            store.clear()
        }
        assertFalse("cancelled session must not resolve an alternate", deadRefreshed.await(4, TimeUnit.SECONDS))
        assertEquals(1L, nextResolved.count)
    }

    @Test fun selectedSourceRefreshFailureStillReachesCaller() = runBlocking {
        try {
            repo.resolvePlayUrl(LineInfo("douban:fixture", "unavailable alternate", "", "fixture|dead-line|0"))
            fail("selected source errors must remain visible to the selected playback flow")
        } catch (_: SelectedSourceRefreshException) {
            assertEquals(0L, deadRefreshed.count)
        }
    }
}

package com.qiubo.optimaltv.player

import android.content.Context
import android.content.ContextWrapper
import android.graphics.Bitmap
import android.os.SystemClock
import android.util.Log
import android.view.KeyEvent
import androidx.lifecycle.viewModelScope
import androidx.room.Room
import androidx.test.platform.app.InstrumentationRegistry
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.navigation.compose.rememberNavController
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.db.AppDb
import com.qiubo.optimaltv.data.model.LineInfo
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.data.repo.VodRepository
import com.qiubo.optimaltv.focus.FocusHarnessActivity
import com.qiubo.optimaltv.focus.Harness
import com.qiubo.optimaltv.playback.DecoderStats
import com.qiubo.optimaltv.playback.EnginePlayState
import com.qiubo.optimaltv.playback.EngineRegistry
import com.qiubo.optimaltv.playback.MediaEngine
import com.qiubo.optimaltv.ui.components.OtvNav
import com.qiubo.optimaltv.ui.player.PlayerScreen
import com.qiubo.optimaltv.ui.player.PlayerUiState
import com.qiubo.optimaltv.ui.player.PlayerViewModel
import com.qiubo.optimaltv.ui.theme.OptimalTvTheme
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import java.io.File
import java.lang.reflect.Method
import java.lang.reflect.InvocationTargetException
import java.util.concurrent.TimeUnit
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicReference
import kotlin.coroutines.Continuation
import kotlin.coroutines.intrinsics.suspendCoroutineUninterceptedOrReturn

/** Long-running, real-renderer VOD coverage; the host supplies a local HLS fixture via adb reverse. */
class VodPlaybackEmulatorTest {
    @get:Rule val rule = createAndroidComposeRule<FocusHarnessActivity>()

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context by lazy { instrumentation.targetContext }

    private lateinit var db: AppDb
    private lateinit var settingsContext: IsolatedSettingsContext
    private lateinit var settings: SettingsStore
    private lateinit var vm: PlayerViewModel
    private lateinit var engine: MediaEngine
    private lateinit var engineId: String
    private var seekKeyDownTime = 0L

    @Test fun media3PlaysThroughAndRemoteFastForwardCommitsAfterHold() = runEngine("media3")

    @Test fun vlcPlaysThroughAndRemoteFastForwardCommitsAfterHold() = runEngine("vlc")

    private fun runEngine(id: String) {
        setUpSession(id)
        startRealSession()
        awaitFirstFrame()

        val playbackStart = SystemClock.elapsedRealtime()
        val snapshots = observeFor(PLAYBACK_WINDOW_MS, captureScreenshots = true)
        assertTrue("$id must play continuously for at least 200 seconds", SystemClock.elapsedRealtime() - playbackStart >= PLAYBACK_WINDOW_MS)
        assertTrue("$id timeline must advance during the soak", snapshots.last().positionMs - snapshots.first().positionMs >= 180_000L)
        if (id == "media3") {
            assertTrue("$id rendered video must advance", snapshots.last().renderedFrames > snapshots.first().renderedFrames)
        }

        val targetMs = holdRightAndAssertAcceleration()
        awaitSeekAndVerifyRecovery(targetMs)
        Log.i(TAG, "engine=$id passed 200s playback + accelerated seek; pos=${engine.uiState.value.positionMs}ms")
    }

    private fun setUpSession(id: String) {
        engineId = id
        Graph.init(context)
        db = Room.inMemoryDatabaseBuilder(context, AppDb::class.java).build()
        settingsContext = IsolatedSettingsContext(context, id)
        settings = SettingsStore(settingsContext)
        val repo = VodRepository(settings, OkHttpClient(), context)

        instrumentation.runOnMainSync {
            Harness.scene = null
            OtvNav.resetForTest()
            vm = PlayerViewModel(VOD_ID, 0, repo, db, settings, autoBoot = false)
            engine = EngineRegistry.create(id, Graph.appContext)
            invokeInstallEngine(vm, engine)
            updateViewModelState(
                vm,
                PlayerUiState(
                    title = "本地长时播放测试",
                    isMovie = true,
                    isLive = false,
                    lines = listOf(LineInfo(VOD_ID, "QA HLS fixture", FIXTURE_URL)),
                    engineId = engine.engineId,
                    engineName = engine.displayName,
                ),
            )
            invokeCollectEngineState(vm)
            rule.activity.viewModelStore.put(VM_KEY, vm)
            Harness.scene = {
                OptimalTvTheme {
                    PlayerScreen(rememberNavController(), VOD_ID, 0)
                }
            }
        }
        rule.waitForIdle()
        instrumentation.runOnMainSync {
            assertNotNull("$id render view must be attached to PlayerScreen", engine.renderView.parent)
        }
        Log.i(TAG, "engine=$id test session installed, fixture=$FIXTURE_URL")
    }

    private fun startRealSession() {
        val completed = CountDownLatch(1)
        val failure = AtomicReference<Throwable?>(null)
        instrumentation.runOnMainSync {
            vm.viewModelScope.launch {
                try {
                    invokeStartSession(vm)
                } catch (t: Throwable) {
                    failure.set(t)
                } finally {
                    completed.countDown()
                }
            }
        }
        assertTrue("$engineId startSession must prepare the fixture", completed.await(30, TimeUnit.SECONDS))
        failure.get()?.let { throw AssertionError("$engineId startSession failed", it) }
    }

    private fun awaitFirstFrame() {
        awaitCondition(60_000L, "$engineId must reach READY and render its first frame") {
            val state = engine.uiState.value
            state.state == EnginePlayState.READY && state.isPlaying && state.firstFrameRendered &&
                vm.ui.value.engineId == engineId && state.durationMs >= 1_000_000L
        }
        val ready = engine.uiState.value
        assertEquals(EnginePlayState.READY, ready.state)
        assertTrue(ready.isPlaying)
        assertTrue(ready.firstFrameRendered)
        assertTrue("unexpected fixture duration ${ready.durationMs}ms", ready.durationMs >= 1_000_000L)
        Log.i(TAG, "engine=$engineId READY firstFrame=${ready.firstFrameRendered} pos=${ready.positionMs} duration=${ready.durationMs}")
    }

    private data class PlaybackSample(
        val elapsedMs: Long,
        val positionMs: Long,
        val renderedFrames: Int,
        val state: EnginePlayState,
    )

    private fun observeFor(durationMs: Long, captureScreenshots: Boolean): List<PlaybackSample> {
        val start = SystemClock.elapsedRealtime()
        var nextLogAt = 20_000L
        var lastAdvancedAt = start
        var lastPosition = engine.uiState.value.positionMs
        var middleShot: Bitmap? = null
        val samples = mutableListOf<PlaybackSample>()

        while (SystemClock.elapsedRealtime() - start < durationMs) {
            val elapsed = SystemClock.elapsedRealtime() - start
            val sample = readSample(elapsed)
            samples += sample
            assertNotEqualsError(sample.state, elapsed)
            assertTrue("$engineId stopped playing at ${sample.positionMs}ms / ${elapsed}ms", engine.uiState.value.isPlaying)
            assertTrue("$engineId lost first-frame signal at ${elapsed}ms", engine.uiState.value.firstFrameRendered)
            assertNull("$engineId playback entered fatal UI state", vm.ui.value.fatalMsg)

            if (sample.positionMs > lastPosition) {
                lastPosition = sample.positionMs
                lastAdvancedAt = SystemClock.elapsedRealtime()
            }
            assertTrue("$engineId timeline stalled for more than 12s at ${sample.positionMs}ms", SystemClock.elapsedRealtime() - lastAdvancedAt < 12_000L)

            if (captureScreenshots && middleShot == null && elapsed >= durationMs / 2) {
                middleShot = captureScreenshot("middle")
                Log.i(TAG, "engine=$engineId middle screenshot saved at pos=${sample.positionMs}ms")
            }
            if (elapsed >= nextLogAt) {
                Log.i(TAG, "engine=$engineId soak=${elapsed / 1000}s state=${sample.state} playing=${engine.uiState.value.isPlaying} pos=${sample.positionMs}ms frames=${sample.renderedFrames}")
                nextLogAt += 20_000L
            }
            SystemClock.sleep(SAMPLE_INTERVAL_MS)
        }

        val final = readSample(durationMs)
        samples += final
        assertEquals("$engineId must finish the soak READY", EnginePlayState.READY, final.state)
        if (captureScreenshots) {
            if (middleShot == null) middleShot = captureScreenshot("middle")
            val endShot = captureScreenshot("end")
            if (engineId == "vlc") {
                assertFalse("VLC video pixels must change between middle and end", sameVideoRegion(middleShot!!, endShot))
            }
        }
        return samples
    }

    private fun readSample(elapsedMs: Long): PlaybackSample {
        val stats = AtomicReference<DecoderStats?>(null)
        instrumentation.runOnMainSync { stats.set(engine.decoderStats()) }
        return PlaybackSample(
            elapsedMs = elapsedMs,
            positionMs = engine.uiState.value.positionMs,
            renderedFrames = stats.get()?.rendered ?: -1,
            state = engine.uiState.value.state,
        )
    }

    private fun holdRightAndAssertAcceleration(): Long {
        val initial = vm.ui.value.positionMs
        assertTrue("seek regression must start well past the first second: $initial", initial > 30_000L)
        val downTime = SystemClock.uptimeMillis()
        seekKeyDownTime = downTime
        sendDpadEvent(downTime, downTime, KeyEvent.ACTION_DOWN, 0)

        var previousTarget = initial
        var expectedTarget = initial + SEEK_STEP_MS
        assertEquals("initial RIGHT should seek by 10s", expectedTarget, vm.ui.value.positionMs)
        previousTarget = vm.ui.value.positionMs
        var repeatCount = 0
        var sawAcceleratedRepeat = false
        try {
            while (SystemClock.uptimeMillis() - downTime < HOLD_MS) {
                SystemClock.sleep(REPEAT_INTERVAL_MS)
                val eventTime = SystemClock.uptimeMillis()
                val heldMs = eventTime - downTime
                repeatCount++
                sendDpadEvent(downTime, eventTime, KeyEvent.ACTION_DOWN, repeatCount)

                val step = if (heldMs >= ACCELERATION_AFTER_MS) SEEK_STEP_MS * 2 else SEEK_STEP_MS
                expectedTarget += step
                assertEquals("held=${heldMs}ms RIGHT increment", expectedTarget, vm.ui.value.positionMs)
                assertTrue("seek intent must keep advancing, never rewind to the first second", vm.ui.value.positionMs > previousTarget)
                previousTarget = vm.ui.value.positionMs
                if (heldMs >= ACCELERATION_AFTER_MS) sawAcceleratedRepeat = true
            }
        } finally {
            sendDpadEvent(downTime, SystemClock.uptimeMillis(), KeyEvent.ACTION_UP, repeatCount)
            seekKeyDownTime = 0L
        }
        assertTrue("hold must exercise the accelerated tier", sawAcceleratedRepeat)
        assertTrue("held RIGHT must move far ahead", expectedTarget > initial + 100_000L)
        Log.i(TAG, "engine=$engineId held RIGHT ${HOLD_MS}ms, target=$expectedTarget repeats=$repeatCount")
        return expectedTarget
    }

    private fun awaitSeekAndVerifyRecovery(targetMs: Long) {
        awaitCondition(20_000L, "$engineId must seek to the held RIGHT target and resume") {
            val state = engine.uiState.value
            state.state == EnginePlayState.READY && state.isPlaying && state.firstFrameRendered &&
                kotlin.math.abs(state.positionMs - targetMs) <= SEEK_TOLERANCE_MS
        }
        val recoveredAt = engine.uiState.value.positionMs
        val observeUntil = SystemClock.elapsedRealtime() + POST_SEEK_OBSERVE_MS
        while (SystemClock.elapsedRealtime() < observeUntil) {
            val state = engine.uiState.value
            assertNotEqualsError(state.state, SystemClock.elapsedRealtime())
            assertTrue("$engineId did not resume playback after seek", state.isPlaying)
            assertTrue("$engineId lost the rendered frame after seek", state.firstFrameRendered)
            SystemClock.sleep(SAMPLE_INTERVAL_MS)
        }
        val recovered = engine.uiState.value
        assertEquals(EnginePlayState.READY, recovered.state)
        assertTrue("$engineId should continue moving after seek", recovered.positionMs >= recoveredAt + 3_000L)
        assertTrue("$engineId seek must not snap back to the opening second", recovered.positionMs > 1_000L)
        Log.i(TAG, "engine=$engineId seek recovery observed for ${POST_SEEK_OBSERVE_MS}ms target=$targetMs actual=${recovered.positionMs}")
    }

    private fun sendDpadEvent(downTime: Long, eventTime: Long, action: Int, repeatCount: Int) {
        val event = KeyEvent(
            downTime,
            eventTime,
            action,
            KeyEvent.KEYCODE_DPAD_RIGHT,
            repeatCount,
            0,
            KeyEvent.KEYCODE_UNKNOWN,
            0,
            0,
            android.view.InputDevice.SOURCE_KEYBOARD,
        )
        instrumentation.sendKeySync(event)
    }

    private fun captureScreenshot(stage: String): Bitmap {
        val bitmap = instrumentation.uiAutomation.takeScreenshot()
        assertNotNull("$engineId $stage screenshot must be captured", bitmap)
        val output = File(context.filesDir, "vod-playback-$engineId-$stage.png")
        output.outputStream().use { bitmap!!.compress(Bitmap.CompressFormat.PNG, 100, it) }
        Log.i(TAG, "engine=$engineId screenshot=$output size=${bitmap!!.width}x${bitmap.height}")
        return bitmap
    }

    private fun sameVideoRegion(a: Bitmap, b: Bitmap): Boolean {
        if (a.width != b.width || a.height != b.height) return false
        val left = a.width / 4
        val top = a.height / 4
        val width = (a.width / 2).coerceAtLeast(1)
        val height = (a.height / 2).coerceAtLeast(1)
        return Bitmap.createBitmap(a, left, top, width, height)
            .sameAs(Bitmap.createBitmap(b, left, top, width, height))
    }

    private fun awaitCondition(timeoutMs: Long, description: String, condition: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (SystemClock.elapsedRealtime() < deadline) {
            if (condition()) return
            SystemClock.sleep(100L)
        }
        assertTrue(description, condition())
    }

    private fun assertNotEqualsError(state: EnginePlayState, elapsedMs: Long) {
        assertTrue("$engineId entered ERROR at ${elapsedMs}ms", state != EnginePlayState.ERROR)
    }

    @After fun tearDown() {
        if (seekKeyDownTime != 0L) {
            val now = SystemClock.uptimeMillis()
            runCatching { sendDpadEvent(seekKeyDownTime, now, KeyEvent.ACTION_UP, 1) }
            seekKeyDownTime = 0L
        }
        instrumentation.runOnMainSync {
            Harness.scene = null
            rule.activity.viewModelStore.clear()
            OtvNav.resetForTest()
        }
        if (::settingsContext.isInitialized) runCatching { settingsContext.deleteIsolatedFiles() }
        if (::db.isInitialized) runCatching { db.close() }
        Log.i(TAG, "engine=$engineId test state cleared")
    }

    private fun invokeInstallEngine(viewModel: PlayerViewModel, newEngine: MediaEngine) {
        PlayerViewModel::class.java.getDeclaredMethod("installEngine", MediaEngine::class.java)
            .apply { isAccessible = true }
            .invoke(viewModel, newEngine)
    }

    private fun invokeCollectEngineState(viewModel: PlayerViewModel) {
        PlayerViewModel::class.java.getDeclaredMethod("collectEngineState")
            .apply { isAccessible = true }
            .invoke(viewModel)
    }

    private fun updateViewModelState(viewModel: PlayerViewModel, state: PlayerUiState) {
        @Suppress("UNCHECKED_CAST")
        val mutable = PlayerViewModel::class.java.getDeclaredField("_ui")
            .apply { isAccessible = true }
            .get(viewModel) as kotlinx.coroutines.flow.MutableStateFlow<PlayerUiState>
        mutable.value = state
    }

    private suspend fun invokeStartSession(viewModel: PlayerViewModel) {
        val method: Method = PlayerViewModel::class.java.getDeclaredMethod(
            "startSession",
            Integer.TYPE,
            java.lang.Long.TYPE,
            java.lang.Boolean.TYPE,
            java.lang.Boolean.TYPE,
            Continuation::class.java,
        ).apply { isAccessible = true }

        try {
            suspendCoroutineUninterceptedOrReturn<Unit> { continuation ->
                method.invoke(viewModel, 0, 0L, false, false, continuation)
            }
        } catch (wrapped: InvocationTargetException) {
            throw wrapped.targetException
        }
    }

    private class IsolatedSettingsContext(base: Context, engineId: String) : ContextWrapper(base) {
        private val isolatedFiles = File(base.cacheDir, "qa-settings-$engineId").apply { mkdirs() }

        override fun getApplicationContext(): Context = this
        override fun getFilesDir(): File = isolatedFiles

        fun deleteIsolatedFiles() {
            isolatedFiles.deleteRecursively()
        }
    }

    companion object {
        private const val TAG = "OTV-TEST"
        private const val VOD_ID = "douban:qa-fixture"
        private const val VM_KEY = "player:douban:qa-fixture:0:0"
        private const val FIXTURE_URL = "http://127.0.0.1:18093/qa.m3u8"
        private const val PLAYBACK_WINDOW_MS = 200_000L
        private const val SAMPLE_INTERVAL_MS = 500L
        private const val SEEK_STEP_MS = 10_000L
        private const val HOLD_MS = 3_000L
        private const val REPEAT_INTERVAL_MS = 100L
        private const val ACCELERATION_AFTER_MS = 2_000L
        private const val POST_SEEK_OBSERVE_MS = 5_000L
        private const val SEEK_TOLERANCE_MS = 8_000L
    }
}

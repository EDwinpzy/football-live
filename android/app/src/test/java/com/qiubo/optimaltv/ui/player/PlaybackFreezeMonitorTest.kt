package com.qiubo.optimaltv.ui.player

import android.app.Application
import android.content.Context
import android.os.Looper
import android.view.View
import com.qiubo.optimaltv.Graph
import com.qiubo.optimaltv.data.analytics.PlaybackTelemetry
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.playback.DecoderStats
import com.qiubo.optimaltv.playback.EnginePlayState
import com.qiubo.optimaltv.playback.EngineUiState
import com.qiubo.optimaltv.playback.MediaEngine
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.LooperMode
import java.time.Duration
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [30])
@LooperMode(LooperMode.Mode.PAUSED)
class PlaybackFreezeMonitorTest {
    private lateinit var context: Context
    private lateinit var settings: SettingsStore
    private lateinit var vm: PlayerViewModel
    private lateinit var engine: FakeMediaEngine
    private lateinit var originalTelemetrySink: (String) -> Unit
    private var originalGraphContext: Context? = null
    private var graphContextCaptured = false

    @Before
    fun setUp() {
        context = RuntimeEnvironment.getApplication()
        ensureGraphContext(context)
        originalTelemetrySink = PlaybackTelemetry.sink
        PlaybackTelemetry.sink = {}

        settings = SettingsStore(context)
        vm = PlayerViewModel(
            "live:freeze-monitor-test",
            settings,
            autoBoot = false,
        )
        engine = FakeMediaEngine(context, "test")
        PlayerViewModel::class.java.getDeclaredMethod("installEngine", MediaEngine::class.java)
            .apply { isAccessible = true }
            .invoke(vm, engine)
        mutableUi().value = mutableUi().value.copy(isLive = true, sourceResolving = false, fatalMsg = null)

        PlayerViewModel::class.java.getDeclaredMethod("startHealthMonitor")
            .apply { isAccessible = true }
            .invoke(vm)
    }

    @After
    fun tearDown() {
        if (::vm.isInitialized) {
            cancelJob("healthJob")
            cancelJob("engineStateJob")
            if (vm.hasEngine()) vm.engine.release()
        }
        if (::originalTelemetrySink.isInitialized) PlaybackTelemetry.sink = originalTelemetrySink
        if (graphContextCaptured) {
            Graph::class.java.getDeclaredField("appContext").apply { isAccessible = true }
                .set(Graph, originalGraphContext)
        }
    }

    @Test
    fun `advancing playback clock with frozen rendered frame count records decode fallback`() {
        // The first sample establishes a baseline; the next three unchanged-frame samples represent
        // the monitor's 30-second freeze window. The loopback backend probe is drained between ticks.
        repeat(4) { sample ->
            advanceOneHealthTick(positionMs = (sample + 1L) * PlayerViewModel.HEALTH_TICK_MS)
        }

        val session = PlayerViewModel::class.java.getDeclaredField("telemetry")
            .apply { isAccessible = true }
            .get(vm) as PlaybackTelemetry.Session
        val decodeFallback = session.recorded().firstOrNull {
            it.stage == PlaybackTelemetry.Stage.ENGINE_FALLBACK &&
                it.errorClass == PlaybackTelemetry.ErrorClass.DECODE
        }

        assertTrue(
            "the real health monitor should classify advancing position with unchanged rendered frames as DECODE",
            decodeFallback != null,
        )
    }

    private fun advanceOneHealthTick(positionMs: Long) {
        engine.setPosition(positionMs)
        val mainLooper = shadowOf(Looper.getMainLooper())
        val readsBefore = engine.decoderStatsReads
        mainLooper.idleFor(Duration.ofMillis(PlayerViewModel.HEALTH_TICK_MS))

        val sampleDeadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2)
        while (engine.decoderStatsReads <= readsBefore && System.nanoTime() < sampleDeadline) {
            Thread.sleep(5)
            mainLooper.idle()
        }
        assertTrue("health monitor did not sample the fake decoder", engine.decoderStatsReads > readsBefore)

        // probeBackendAlive() runs on Dispatchers.IO. Give its local-only socket check time to
        // resume the monitor on the main looper before advancing the next virtual 10-second tick.
        val drainDeadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(350)
        while (System.nanoTime() < drainDeadline) {
            Thread.sleep(5)
            mainLooper.idle()
        }
    }

    @Suppress("UNCHECKED_CAST")
    private fun mutableUi(): MutableStateFlow<PlayerUiState> =
        PlayerViewModel::class.java.getDeclaredField("_ui")
            .apply { isAccessible = true }
            .get(vm) as MutableStateFlow<PlayerUiState>

    private fun cancelJob(fieldName: String) {
        PlayerViewModel::class.java.getDeclaredField(fieldName)
            .apply { isAccessible = true }
            .get(vm)
            ?.let { (it as? Job)?.cancel() }
    }

    private fun ensureGraphContext(context: Context) {
        val field = Graph::class.java.getDeclaredField("appContext").apply { isAccessible = true }
        originalGraphContext = field.get(Graph) as? Context
        graphContextCaptured = true
        if (originalGraphContext == null) field.set(Graph, context.applicationContext)
    }

    private class FakeMediaEngine(
        context: Context,
        override val engineId: String,
    ) : MediaEngine {
        override val displayName: String = "Fake engine"
        override val renderView: View = View(context)

        private val state = MutableStateFlow(
            EngineUiState(
                state = EnginePlayState.READY,
                positionMs = 0L,
                durationMs = 600_000L,
                isPlaying = true,
                firstFrameRendered = true,
            ),
        )
        override val uiState: StateFlow<EngineUiState> = state.asStateFlow()
        var decoderStatsReads: Int = 0
            private set

        fun setPosition(positionMs: Long) {
            state.value = state.value.copy(positionMs = positionMs)
        }

        override fun prepare(request: com.qiubo.optimaltv.playback.PrepareRequest) = Unit
        override fun swapSource(url: String, autoplay: Boolean) = Unit
        override fun seekToLiveEdge() = Unit
        override fun setSoftwareDecoderPreferred(on: Boolean) = Unit
        override fun setLiveWindowHintMs(windowMs: Long, segmentMs: Long) = Unit
        override fun audioCodecs(): String? = null
        override fun decoderStats(): DecoderStats {
            decoderStatsReads++
            return DecoderStats(
                rendered = 100,
                skipped = 0,
                dropped = 0,
                droppedInput = 0,
                decoderInits = 1,
                consecutiveDropped = 0,
            )
        }

        override fun play() = Unit
        override fun pause() = Unit
        override fun setSpeed(v: Float) = Unit
        override fun release() = Unit
    }
}

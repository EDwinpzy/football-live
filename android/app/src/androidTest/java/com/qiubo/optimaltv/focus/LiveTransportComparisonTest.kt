@file:androidx.media3.common.util.UnstableApi

package com.qiubo.optimaltv.focus

import android.os.SystemClock
import android.util.Base64
import android.view.ViewGroup
import android.widget.FrameLayout
import androidx.test.core.app.ActivityScenario
import androidx.test.platform.app.InstrumentationRegistry
import com.qiubo.optimaltv.playback.EnginePlayState
import com.qiubo.optimaltv.playback.Media3Engine
import com.qiubo.optimaltv.playback.PrepareRequest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

/** Opt-in transport diagnostic; sampled buffering can miss brief transitions. */
class LiveTransportComparisonTest {
    @Test fun compareDirectAndRelayOnTheSameDevice() {
        val args = InstrumentationRegistry.getArguments()
        val direct = args.getString("directBase64").orEmpty()
        val relay = args.getString("relayBase64").orEmpty()
        assumeTrue("Explicit live-source arguments required", direct.isNotBlank() && relay.isNotBlank())
        val duration = args.getString("durationSeconds")?.toLongOrNull()?.coerceIn(20, 180) ?: 60L
        val window = args.getString("windowMs")?.toLongOrNull() ?: 32_400L
        val segment = args.getString("segmentMs")?.toLongOrNull() ?: 5_422L
        val expectNoRebuffer = args.getString("expectNoRebuffer") == "true"
        ActivityScenario.launch(FocusHarnessActivity::class.java).use { scenario ->
            for ((label, encoded) in listOf("DIRECT" to direct, "RELAY" to relay)) {
                val url = String(Base64.decode(encoded, Base64.DEFAULT), Charsets.UTF_8)
                lateinit var engine: Media3Engine
                scenario.onActivity { activity ->
                    engine = Media3Engine(activity)
                    activity.addContentView(engine.renderView, FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT,
                    ))
                    engine.setLiveWindowHintMs(window, segment)
                    engine.prepare(PrepareRequest(url))
                }
                var rendered = false
                var rebuffers = 0
                var previous = EnginePlayState.IDLE
                var errors = 0
                fun sample() {
                    val state = engine.uiState.value
                    rendered = rendered || state.firstFrameRendered
                    if (previous == EnginePlayState.READY && state.state == EnginePlayState.BUFFERING) rebuffers++
                    errors = engine.playbackErrorCount()
                    previous = state.state
                }
                val started = SystemClock.elapsedRealtime()
                try {
                    while (SystemClock.elapsedRealtime() - started < duration * 1000L) {
                        scenario.onActivity { sample() }
                        SystemClock.sleep(100)
                    }
                } finally {
                    scenario.onActivity {
                        // Take the final playback snapshot before release changes player state.
                        sample()
                        val counters = engine.decoderStats()
                        val text = "$label rendered=$rendered rebuffers=$rebuffers errors=$errors frames=${counters?.rendered} dropped=${counters?.dropped}"
                        android.util.Log.i("OTV-TRANSPORT", text)
                        println(text)
                        engine.release()
                        (engine.renderView.parent as? ViewGroup)?.removeView(engine.renderView)
                    }
                }
                assertTrue("$label never rendered a frame", rendered)
                assertEquals("$label raised a playback error", 0, errors)
                if (expectNoRebuffer) {
                    assertEquals("$label rebuffered during deterministic HLS fixture", 0, rebuffers)
                }
            }
        }
    }
}

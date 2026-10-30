package com.qiubo.optimaltv.playback

import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment

@RunWith(RobolectricTestRunner::class)
class Media3ErrorStateTest {
    private fun listener(engine: Media3Engine): Player.Listener =
        Media3Engine::class.java.getDeclaredField("listener").let {
            it.isAccessible = true
            it.get(engine) as Player.Listener
        }

    @Test fun errorSurvivesFollowingIdleUntilPlaybackResumes() {
        val engine = Media3Engine(RuntimeEnvironment.getApplication())
        val listener = listener(engine)
        listener.onPlaybackStateChanged(Player.STATE_READY)
        listener.onPlayerError(PlaybackException("source failed", null, PlaybackException.ERROR_CODE_IO_BAD_HTTP_STATUS))
        val error = engine.uiState.value
        assertEquals(EnginePlayState.ERROR, error.state)
        listener.onPlaybackStateChanged(Player.STATE_IDLE)
        assertEquals(error, engine.uiState.value)
        listener.onPlaybackStateChanged(Player.STATE_BUFFERING)
        assertEquals(EnginePlayState.BUFFERING, engine.uiState.value.state)
    }

    @Test fun ordinaryIdleAndLiveWindowRecoveryAreNotErrors() {
        val engine = Media3Engine(RuntimeEnvironment.getApplication())
        val listener = listener(engine)
        listener.onPlaybackStateChanged(Player.STATE_READY)
        listener.onPlaybackStateChanged(Player.STATE_IDLE)
        assertEquals(EnginePlayState.IDLE, engine.uiState.value.state)
        listener.onPlayerError(PlaybackException("behind", null, PlaybackException.ERROR_CODE_BEHIND_LIVE_WINDOW))
        assertEquals(EnginePlayState.BUFFERING, engine.uiState.value.state)
        assertNull(engine.uiState.value.errorMsg)
        listener.onPlaybackStateChanged(Player.STATE_IDLE)
        assertEquals(EnginePlayState.IDLE, engine.uiState.value.state)
    }
}

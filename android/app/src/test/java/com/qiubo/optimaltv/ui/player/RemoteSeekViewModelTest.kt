package com.qiubo.optimaltv.ui.player

import android.app.Application
import android.os.Looper
import androidx.room.Room
import com.qiubo.optimaltv.data.db.AppDb
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.data.repo.VodRepository
import com.qiubo.optimaltv.playback.EngineUiState
import com.qiubo.optimaltv.playback.MediaEngine
import com.qiubo.optimaltv.ui.components.OtvNav
import com.qiubo.optimaltv.ui.components.NAV_RIGHT
import kotlinx.coroutines.flow.MutableStateFlow
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.After
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.lang.reflect.Proxy
import java.time.Duration

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [30])
class RemoteSeekViewModelTest {
    private val context = RuntimeEnvironment.getApplication()
    private val db = Room.inMemoryDatabaseBuilder(context, AppDb::class.java).build()
    private val settings = SettingsStore(context)
    private val vm = PlayerViewModel("douban:test", 0, VodRepository(settings, OkHttpClient(), context), db, settings, autoBoot = false)
    private val seeks = mutableListOf<Long>()
    private val engineState = MutableStateFlow(EngineUiState())

    init {
        OtvNav.resetForTest()
        val engine = Proxy.newProxyInstance(MediaEngine::class.java.classLoader, arrayOf(MediaEngine::class.java)) { proxy, method, args ->
            when (method.name) {
                "getEngineId", "getDisplayName" -> "test"
                "getUiState" -> engineState
                "seekTo" -> { seeks.add(args!![0] as Long); null }
                "equals" -> proxy === args!![0]
                "hashCode" -> System.identityHashCode(proxy)
                else -> null
            }
        } as MediaEngine
        PlayerViewModel::class.java.getDeclaredMethod("installEngine", MediaEngine::class.java).apply { isAccessible = true }.invoke(vm, engine)
        state().value = vm.ui.value.copy(positionMs = 60_000, durationMs = 3_600_000)
    }

    @Suppress("UNCHECKED_CAST")
    private fun state() = PlayerViewModel::class.java.getDeclaredField("_ui").apply { isAccessible = true }.get(vm) as MutableStateFlow<PlayerUiState>

    @After fun close() {
        PlayerViewModel::class.java.getDeclaredField("engineStateJob").apply { isAccessible = true }
            .get(vm).let { (it as? kotlinx.coroutines.Job)?.cancel() }
        db.close()
        OtvNav.resetForTest()
    }

    @Test fun `remote hold duration accelerates VM seeks and next tap resets`() {
        OtvNav.dispatch(NAV_RIGHT, 40, 6_000L)
        vm.seekFromRemote(10_000L)
        assertEquals(140_000L, vm.ui.value.positionMs)
        OtvNav.dispatch(NAV_RIGHT, 0, 6_000L)
        vm.seekFromRemote(10_000L)
        assertEquals(150_000L, vm.ui.value.positionMs)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(300))
        assertEquals(listOf(150_000L), seeks)
    }

    @Test fun `repeats preview immediately but submit one final seek`() {
        repeat(20) {
            vm.seekFromRemote(10_000L)
            shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(65))
        }
        assertEquals(260_000L, vm.ui.value.positionMs)
        assertEquals(emptyList<Long>(), seeks)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(300))
        assertEquals(listOf(260_000L), seeks)
    }

    @Test fun `stale engine samples cannot overwrite queued target or duration`() {
        engineState.value = EngineUiState(positionMs = 60_000L, durationMs = 3_600_000L)
        PlayerViewModel::class.java.getDeclaredMethod("collectEngineState").apply { isAccessible = true }.invoke(vm)
        vm.seekFromRemote(10_000L)
        engineState.value = EngineUiState(positionMs = 0L, durationMs = 0L)
        shadowOf(Looper.getMainLooper()).idle()
        assertEquals(70_000L, vm.ui.value.positionMs)
        assertEquals(3_600_000L, vm.ui.value.durationMs)
        vm.seekFromRemote(10_000L)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(300))
        assertEquals(listOf(80_000L), seeks)
    }

    @Test fun `source selection cancels seek scheduled for the previous source`() {
        vm.seekFromRemote(10_000L)
        PlayerViewModel::class.java.getDeclaredMethod("supersedePendingVodSession").apply { isAccessible = true }.invoke(vm)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(300))
        assertEquals(emptyList<Long>(), seeks)
    }
}

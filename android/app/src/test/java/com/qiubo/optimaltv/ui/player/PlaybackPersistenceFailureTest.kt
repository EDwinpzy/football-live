package com.qiubo.optimaltv.ui.player

import android.app.Application
import androidx.room.Room
import com.qiubo.optimaltv.data.db.AppDb
import com.qiubo.optimaltv.data.db.HistoryEntity
import com.qiubo.optimaltv.data.db.ProgressEntity
import com.qiubo.optimaltv.data.model.VodItem
import com.qiubo.optimaltv.data.prefs.SettingsStore
import com.qiubo.optimaltv.data.repo.VodRepository
import com.qiubo.optimaltv.playback.EnginePlayState
import kotlinx.coroutines.Job
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.lang.reflect.InvocationTargetException
import kotlin.coroutines.Continuation
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine
import kotlin.coroutines.intrinsics.COROUTINE_SUSPENDED

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [30])
class PlaybackPersistenceFailureTest {
    private val context = RuntimeEnvironment.getApplication()
    private val db = Room.inMemoryDatabaseBuilder(context, AppDb::class.java)
        .allowMainThreadQueries()
        .build()
    private val settings = SettingsStore(context)
    private val vm = PlayerViewModel(
        "douban:persistence-test",
        0,
        VodRepository(settings, OkHttpClient(), context),
        db,
        settings,
        autoBoot = false,
    )
    private val item = VodItem(
        id = "douban:persistence-test",
        sourceId = "douban",
        title = "Persistence failure test",
        categoryId = "douban:tv",
    )

    @Before
    fun prepareReadyPlayback() {
        PlayerViewModel::class.java.getDeclaredField("currentItem").apply { isAccessible = true }
            .set(vm, item)
        uiState().value = vm.ui.value.copy(
            title = item.title,
            playState = EnginePlayState.READY,
            isPlaying = true,
            positionMs = 181_000L,
            durationMs = 3_600_000L,
            fatalMsg = null,
            isLive = false,
        )
    }

    @After
    fun close() {
        (PlayerViewModel::class.java.getDeclaredField("progressJob").apply { isAccessible = true }
            .get(vm) as? Job)?.cancel()
        db.close()
    }

    @Test
    fun `repeated Room progress write failures do not escape or change playback state`() = runBlocking {
        failInsertsInto("progress")
        assertRoomWriteFails {
            db.vodDao().upsertProgress(
                ProgressEntity(item.id, item.id, 0, 181_000L, 3_600_000L, 1L),
            )
        }

        val expectedState = vm.ui.value
        repeat(3) {
            invokeSuspend("saveProgress", false, expectedState)
            assertEquals(expectedState, vm.ui.value)
            assertEquals(EnginePlayState.READY, vm.ui.value.playState)
            assertEquals(true, vm.ui.value.isPlaying)
        }
    }

    @Test
    fun `initial history write failure does not escape READY callback`() = runBlocking {
        failInsertsInto("history")
        assertRoomWriteFails {
            db.vodDao().upsertHistory(
                HistoryEntity(item.id, item.title, item.posterUrl, 0, 181_000L, 3_600_000L, 1L),
            )
        }

        val expectedState = vm.ui.value
        invokeSuspend("onSessionStarted", 181_000L)

        assertEquals(expectedState, vm.ui.value)
        assertEquals(EnginePlayState.READY, vm.ui.value.playState)
        assertEquals(true, vm.ui.value.isPlaying)
        assertEquals(true, PlayerViewModel::class.java.getDeclaredField("startedThisSession")
            .apply { isAccessible = true }.getBoolean(vm))
    }

    private fun failInsertsInto(table: String) {
        require(table == "progress" || table == "history")
        db.openHelper.writableDatabase.execSQL(
            "CREATE TRIGGER fail_${table}_insert BEFORE INSERT ON $table " +
                "BEGIN SELECT RAISE(ABORT, 'forced test write failure'); END",
        )
    }

    private suspend fun assertRoomWriteFails(write: suspend () -> Unit) {
        val failure = runCatching { write() }.exceptionOrNull()
        assertNotNull("the Room trigger should force an actual SQLite write failure", failure)
    }

    @Suppress("UNCHECKED_CAST")
    private fun uiState() = PlayerViewModel::class.java.getDeclaredField("_ui")
        .apply { isAccessible = true }
        .get(vm) as kotlinx.coroutines.flow.MutableStateFlow<PlayerUiState>

    private suspend fun invokeSuspend(methodName: String, vararg arguments: Any?) {
        val method = PlayerViewModel::class.java.declaredMethods.single {
            it.name == methodName && it.parameterTypes.lastOrNull() == Continuation::class.java
        }.apply { isAccessible = true }

        suspendCoroutine<Unit> { continuation ->
            val invocationArguments = arrayOfNulls<Any?>(arguments.size + 1)
            arguments.forEachIndexed { index, argument -> invocationArguments[index] = argument }
            invocationArguments[arguments.size] = continuation
            try {
                val result = method.invoke(vm, *invocationArguments)
                if (result !== COROUTINE_SUSPENDED) continuation.resume(Unit)
            } catch (failure: InvocationTargetException) {
                continuation.resumeWithException(failure.targetException)
            } catch (failure: Exception) {
                continuation.resumeWithException(failure)
            }
        }
    }
}

package com.qiubo.optimaltv.data.prefs

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.floatPreferencesKey
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "settings")

/** 画幅模式（技术方案 §3.4 画面比例切换） */
enum class AspectMode(val label: String, val ratio: Float?) {
    FIT("原始", null),
    ZOOM("裁切填充", null),
    R16_9("16:9", 16f / 9f),
    R4_3("4:3", 4f / 3f),
    ;
    companion object { fun of(i: Int) = entries.getOrElse(i) { FIT } }
}

data class AppSettings(
    /** 做直播 API。 */
    val liveApiBaseUrl: String = "http://127.0.0.1:8090",
    val engineId: String = "media3",
    val aspectOrdinal: Int = 0,
    val speed: Float = 1.0f,
)

class SettingsStore(private val context: Context) {

    private object K {
        val LIVE_API_BASE = stringPreferencesKey("live_api_base")
        val ENGINE = stringPreferencesKey("engine_id")
        val ASPECT = intPreferencesKey("aspect_mode")
        val SPEED = floatPreferencesKey("speed")
    }

    val settings: Flow<AppSettings> = context.dataStore.data.map { p ->
        AppSettings(
            liveApiBaseUrl = p[K.LIVE_API_BASE] ?: "http://127.0.0.1:8090",
            engineId = p[K.ENGINE] ?: "media3",
            aspectOrdinal = p[K.ASPECT] ?: 0,
            speed = p[K.SPEED] ?: 1.0f,
        )
    }

    suspend fun current(): AppSettings = settings.first()

    suspend fun setEngine(id: String) = context.dataStore.edit { it[K.ENGINE] = id }
    suspend fun setAspect(ord: Int) = context.dataStore.edit { it[K.ASPECT] = ord }
    suspend fun setSpeed(v: Float) = context.dataStore.edit { it[K.SPEED] = v }

    // 做直播：保留用户上次手动选定的线路标识；历史旧源由 PlayerViewModel 回退到 A2。
    private val LIVE_LAST_SRC = stringPreferencesKey("live_last_src")

    suspend fun liveLastSrc(): String? = context.dataStore.data.first()[LIVE_LAST_SRC]

    suspend fun rememberLiveLastSrc(src: String) {
        context.dataStore.edit { it[LIVE_LAST_SRC] = src }
    }
}

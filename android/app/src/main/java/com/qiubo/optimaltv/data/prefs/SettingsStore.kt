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
    val apiBaseUrl: String = "",
    /** hhkan 聚合 API。默认指向 app 内置后端（EmbeddedBackend 本地跑，127.0.0.1）。
     * 兼容外部后端：仍可在设置页改成其他局域网 IP。 */
    val hhkanBaseUrl: String = "http://127.0.0.1:8090",
    val engineId: String = "media3",
    val aspectOrdinal: Int = 0,
    val speed: Float = 1.0f,
)

class SettingsStore(private val context: Context) {

    private object K {
        val API_BASE = stringPreferencesKey("api_base_url")
        val HHKAN_BASE = stringPreferencesKey("hhkan_base_url")
        val ENGINE = stringPreferencesKey("engine_id")
        val ASPECT = intPreferencesKey("aspect_mode")
        val SPEED = floatPreferencesKey("speed")
    }

    val settings: Flow<AppSettings> = context.dataStore.data.map { p ->
        AppSettings(
            apiBaseUrl = p[K.API_BASE] ?: "",
            hhkanBaseUrl = p[K.HHKAN_BASE] ?: "http://127.0.0.1:8090",
            engineId = p[K.ENGINE] ?: "media3",
            aspectOrdinal = p[K.ASPECT] ?: 0,
            speed = p[K.SPEED] ?: 1.0f,
        )
    }

    suspend fun current(): AppSettings = settings.first()

    suspend fun setApiBase(url: String) = context.dataStore.edit { it[K.API_BASE] = url.trim() }
    suspend fun setHhkanBase(url: String) = context.dataStore.edit { it[K.HHKAN_BASE] = url.trim() }
    suspend fun setEngine(id: String) = context.dataStore.edit { it[K.ENGINE] = id }
    suspend fun setAspect(ord: Int) = context.dataStore.edit { it[K.ASPECT] = ord }
    suspend fun setSpeed(v: Float) = context.dataStore.edit { it[K.SPEED] = v }

    // ---- 线路记忆（技术方案 §3.2-3：每部影片上次成功的线路，下次直选）----
    private fun lineKey(vodId: String) = stringPreferencesKey("line:$vodId")

    /** 记录格式 "engineId|lineIndex" */
    suspend fun rememberLine(vodId: String, engineId: String, lineIndex: Int) {
        context.dataStore.edit { it[lineKey(vodId)] = "$engineId|$lineIndex" }
    }

    suspend fun lastLine(vodId: String): Pair<String, Int>? =
        context.dataStore.data.first()[lineKey(vodId)]?.split("|")?.let {
            if (it.size == 2) it[0] to (it[1].toIntOrNull() ?: 0) else null
        }

    // ---- IPTV 上次观看频道（v1.15 电视页「进入即播」）----
    // 三个独立 key（分组名/频道名/URL 各存各的）：避免自行拼分隔符与频道名、URL 中的
    // 特殊字符打架；恢复时优先 name+url 精确匹配，次选仅 name 匹配（同频道 URL 已更新）
    private object IptvLast {
        val GROUP = stringPreferencesKey("iptv_last_group")
        val NAME = stringPreferencesKey("iptv_last_name")
        val URL = stringPreferencesKey("iptv_last_url")
    }

    /** 上次观看频道（group/name/url），无历史返回 null */
    suspend fun iptvLast(): Triple<String, String, String>? =
        context.dataStore.data.first().let { p ->
            val name = p[IptvLast.NAME] ?: return null
            val url = p[IptvLast.URL] ?: return null
            Triple(p[IptvLast.GROUP].orEmpty(), name, url)
        }

    suspend fun rememberIptvLast(group: String, name: String, url: String) {
        context.dataStore.edit {
            it[IptvLast.GROUP] = group
            it[IptvLast.NAME] = name
            it[IptvLast.URL] = url
        }
    }

    // ---- 全部影视页：上次选中类别（v1.16 需求：进页强制选中上次类别，不回「全部」）----
    private val ALL_LAST_CAT = stringPreferencesKey("all_last_cat")

    suspend fun allLastCat(): String? = context.dataStore.data.first()[ALL_LAST_CAT]

    suspend fun rememberAllLastCat(cat: String) {
        context.dataStore.edit { it[ALL_LAST_CAT] = cat }
    }

    // ---- 影视首页：上次选中分类 chip（v1.17 需求：进页恢复上次分类，不回「电影」）----
    private val VOD_LAST_CAT = stringPreferencesKey("vod_last_cat")

    suspend fun vodLastCat(): String? = context.dataStore.data.first()[VOD_LAST_CAT]

    suspend fun rememberVodLastCat(cat: String) {
        context.dataStore.edit { it[VOD_LAST_CAT] = cat }
    }

    // ---- 足球直播：上次手动选择的信号源标识（2026-09-07 需求③：进直播默认直选；
    // 旧源记忆由 VM 迁移到新线路默认 A2）----
    private val LIVE_LAST_SRC = stringPreferencesKey("live_last_src")

    suspend fun liveLastSrc(): String? = context.dataStore.data.first()[LIVE_LAST_SRC]

    suspend fun rememberLiveLastSrc(src: String) {
        context.dataStore.edit { it[LIVE_LAST_SRC] = src }
    }
}

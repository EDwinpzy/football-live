package com.qiubo.optimaltv.announcement

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringSetPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.qiubo.optimaltv.BuildConfig
import com.qiubo.optimaltv.OtvLog
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.cancelChildren
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.TimeUnit

private val Context.announceStore by preferencesDataStore(name = "announcement")

/**
 * v1.20 运营公告（用户需求：后台管理系统发送公告推送到 app，顶部半透明悬浮窗展示）。
 *
 * 链路：后台发布 → PG announcements 表（published 单条生效）→ hotupdate 云函数
 * GET /announce（公开）→ app 进入前台拉取 + 前台驻留期低频轮询 → StateFlow 驱动
 * 顶部悬浮窗。用户关闭公告 = 记住该公告 id 不再弹（后台发新公告换 id 再弹）。
 *
 * 非阻塞：拉取失败/断网静默跳过，绝不影响任何页面使用。
 */
object AnnouncementManager {

    data class Announcement(val id: Int, val title: String, val content: String)

    private object K {
        // 需求 会员#9：本机提示过一次就不再提示——展示瞬间即落盘（旧版只在关闭时记录，
        // 20s 展示期内杀进程/重启会重复弹）。旧 DISMISSED 单 id 保留兼容读取（并集迁移）
        // 2026-09-05 用户需求「弹出过一次就不要再弹出」：改存【内容指纹】（标题+正文 MD5）——
        // announcements 表每次发布都生成新 id，同内容重复发布按 id 去重会误判为新公告再弹；
        // 旧集合里存的数字 id 保留并集（永不命中 16 位指纹，无害）
        val SHOWN = stringSetPreferencesKey("shown_ids")
        val DISMISSED = intPreferencesKey("dismissed_id")
    }

    /** PostgreSQL CU 降耗：公告只在前台每 5 分钟检查一次。
     * 云函数端还有 5 分钟合并缓存，因此多台设备不会继续按分钟打数据库。 */
    private const val POLL_MS = 5 * 60_000L
    private const val LOOP_TICK_MS = 60_000L

    private val _current = MutableStateFlow<Announcement?>(null)

    /** 当前应展示的公告（无/已关闭/拉取失败 = null）——UI 悬浮窗直接订阅 */
    val current: StateFlow<Announcement?> = _current.asStateFlow()

    @Volatile private var inited = false
    @Volatile private var foreground = false
    @Volatile private var lastPollStartedMs = 0L
    private val pollInFlight = AtomicBoolean(false)
    private lateinit var appCtx: Context
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val http = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        .readTimeout(8, TimeUnit.SECONDS)
        .build()

    /** App.onCreate 调用：启动本地调度器；实际联网由前台状态门控。 */
    fun init(context: Context) {
        if (inited) return
        inited = true
        appCtx = context.applicationContext
        if (BuildConfig.HOTUPDATE_ENDPOINT.isBlank()) return
        scope.launch {
            while (true) {
                runCatching { pollIfDue() }
                delay(LOOP_TICK_MS)
            }
        }
    }

    /** MainActivity onResume/onPause 调用：后台完全停止公告联网。 */
    fun onForegroundChanged(value: Boolean) {
        foreground = value
        if (value && this::appCtx.isInitialized) scope.launch { pollIfDue() }
    }

    /** 用户明确退出时停止轮询；普通切后台只由 foreground 门控，不销毁。 */
    fun shutdown() {
        foreground = false
        scope.coroutineContext.cancelChildren()
        _current.value = null
    }

    private suspend fun pollIfDue() {
        if (!foreground) return
        val now = android.os.SystemClock.elapsedRealtime()
        if (lastPollStartedMs > 0L && now - lastPollStartedMs < POLL_MS) return
        if (!pollInFlight.compareAndSet(false, true)) return
        lastPollStartedMs = now
        try {
            pollOnce()
        } finally {
            pollInFlight.set(false)
        }
    }

    /** 收起当前悬浮窗（✕ / 20s 自动收起）：不再提示由展示时落盘的 SHOWN 保证 */
    fun dismiss() {
        _current.value = null
    }

    /** 内容指纹（标题+正文 MD5 前 16 位）——同内容无论后台重复发布几次（换新 id），本机只弹一次 */
    private fun contentKey(a: Announcement): String =
        java.security.MessageDigest.getInstance("MD5")
            .digest((a.title + "\u0001" + a.content).toByteArray())
            .joinToString("") { "%02x".format(it) }
            .take(16)

    private suspend fun pollOnce() {
        val ann = fetchLatest() ?: run {
            OtvLog.w("announcement: fetch null（网络/解析失败或超时）")
            return        // 无公告/网络失败：保持现状
        }
        if (ann.id <= 0) return
        val key = contentKey(ann)
        val prefs = appCtx.announceStore.data.first()
        val seen = prefs[K.SHOWN].orEmpty() +
            (prefs[K.DISMISSED]?.toString()?.let { setOf(it) } ?: emptySet())
        if (key in seen) {
            OtvLog.i("announcement: 「${ann.title}」内容本机已提示过，跳过")
            return
        }
        val cur = _current.value
        if (cur == null || cur.id != ann.id) {
            _current.value = ann
            // 展示即落盘（需求 会员#9）：存内容指纹——同内容换 id 重发也不再弹
            runCatching { appCtx.announceStore.edit { it[K.SHOWN] = seen + key } }
            OtvLog.i("announcement: 展示 #${ann.id} ${ann.title}")
        }
    }

    private suspend fun fetchLatest(): Announcement? = withContext(Dispatchers.IO) {
        runCatching {
            val req = Request.Builder()
                .url(BuildConfig.HOTUPDATE_ENDPOINT.trimEnd('/') + "/announce")
                // 自有站点接口统一浏览器 UA：见 data/net/HttpUa（非浏览器 UA 会被边缘拦成 403）
                .header("User-Agent", com.qiubo.optimaltv.data.net.HttpUa.BROWSER)
                .header("Accept", com.qiubo.optimaltv.data.net.HttpUa.ACCEPT)
                .get()
                .build()
            withTimeoutOrNull(10_000) {
                http.newCall(req).execute().use { r ->
                    OtvLog.i("announcement: http ${r.code}")
                    if (!r.isSuccessful) return@withTimeoutOrNull null
                    val jo = JSONObject(r.body?.string().orEmpty())
                    if (jo.optInt("ret", -1) != 0) return@withTimeoutOrNull null
                    val a = jo.optJSONObject("ann") ?: return@withTimeoutOrNull null
                    Announcement(
                        id = a.optInt("id", 0),
                        title = a.optString("title", "").trim().take(60),
                        content = a.optString("content", "").trim().take(600),
                    ).takeIf { it.id > 0 && it.title.isNotBlank() }
                }
            }
        }.onFailure { OtvLog.w("announcement: fetch 异常 ${it.message}") }.getOrNull()
    }
}

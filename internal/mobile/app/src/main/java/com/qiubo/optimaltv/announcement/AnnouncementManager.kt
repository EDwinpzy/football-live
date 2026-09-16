package com.qiubo.optimaltv.announcement

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
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
 * v1.20 运营公告；v1.25（2026-09-07 需求⑪）：移动版改为【系统级通知】送达——
 * 新公告发到状态栏通知（点按回 app），不再 app 内顶部悬浮窗（TV 版仍为悬浮窗，
 * 双端本文件各自维护）。
 *
 * 链路：后台发布 → PG announcements 表（published 单条生效）→ hotupdate 云函数
 * GET /announce（公开）→ app 进入前台拉取 + 前台驻留期低频轮询 → 系统通知。
 * 每条内容指纹只通知一次（后台同内容换 id 重发不再扰）。
 *
 * 非阻塞：拉取失败/断网静默跳过，绝不影响任何页面使用。
 */
object AnnouncementManager {

    data class Announcement(val id: Int, val title: String, val content: String)

    private object K {
        // 2026-09-05 用户需求「弹出过一次就不要再弹出」：按【内容指纹】去重——
        // announcements 表每次发布都生成新 id，同内容重复发布按 id 去重会误判为新公告再弹；
        // 旧集合里存的数字 id 保留并集（永不命中 16 位指纹，无害）
        val SHOWN = stringSetPreferencesKey("shown_ids")
        val DISMISSED = intPreferencesKey("dismissed_id")
    }

    /** PostgreSQL CU 降耗：公告只在前台每 5 分钟检查一次。 */
    private const val POLL_MS = 5 * 60_000L
    private const val LOOP_TICK_MS = 60_000L

    private const val CHANNEL_ID = "otv_announce"
    private const val NOTIFY_ID_BASE = 4700

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

    fun shutdown() {
        foreground = false
        scope.coroutineContext.cancelChildren()
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

    /** 内容指纹（标题+正文 MD5 前 16 位）：同内容无论发布几次本机只通知一次 */
    private fun contentKey(a: Announcement): String =
        java.security.MessageDigest.getInstance("MD5")
            .digest((a.title + "\u0001" + a.content).toByteArray())
            .joinToString("") { "%02x".format(it) }
            .take(16)

    private suspend fun pollOnce() {
        val ann = fetchLatest() ?: return        // 无公告/网络失败：保持现状
        if (ann.id <= 0) return
        val key = contentKey(ann)
        val prefs = appCtx.announceStore.data.first()
        val seen = prefs[K.SHOWN].orEmpty() +
            (prefs[K.DISMISSED]?.toString()?.let { setOf(it) } ?: emptySet())
        if (key in seen) return
        // 需求⑪：系统通知送达（状态栏），通知过即落盘指纹
        notifySystem(ann)
        runCatching { appCtx.announceStore.edit { it[K.SHOWN] = seen + key } }
        OtvLog.i("announcement: 系统通知 #${ann.id} ${ann.title}")
    }

    /** 状态栏通知（Android 13+ 需 POST_NOTIFICATIONS 运行时权限，MainActivity 申请） */
    private fun notifySystem(a: Announcement) {
        val nm = appCtx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (android.os.Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL_ID) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "运营公告", NotificationManager.IMPORTANCE_DEFAULT).apply {
                    description = "My TV 运营公告推送"
                }
            )
        }
        val launch = appCtx.packageManager.getLaunchIntentForPackage(appCtx.packageName)
        val pi = PendingIntent.getActivity(
            appCtx, 0, launch,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val n = androidx.core.app.NotificationCompat.Builder(appCtx, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(a.title)
            .setContentText(a.content)
            .setStyle(androidx.core.app.NotificationCompat.BigTextStyle().bigText(a.content))
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build()
        runCatching { nm.notify(NOTIFY_ID_BASE + (a.id % 1000), n) }
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
        }.getOrNull()
    }
}

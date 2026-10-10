package com.qiubo.optimaltv.license

import android.annotation.SuppressLint
import android.content.Context
import android.os.Build
import android.provider.Settings
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.qiubo.optimaltv.BuildConfig
import com.qiubo.optimaltv.OtvLog
import kotlinx.coroutines.CoroutineScope
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
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.security.KeyFactory
import java.security.PublicKey
import java.security.Signature
import java.security.spec.X509EncodedKeySpec
import java.time.Instant
import java.time.OffsetDateTime
import java.util.Base64
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

private val Context.licenseStore by preferencesDataStore(name = "license")

/** 服务端授权状态（expiryAt == null 表示终身）。 */
data class LicenseActivated(
    val code: String,
    val plan: String,
    val days: Int,
    val expiryAt: Long?,
    val activatedAt: Long,
)

sealed interface LicenseState {
    data object NotActivated : LicenseState
    data class Activated(val data: LicenseActivated) : LicenseState
}

sealed interface ActivateResult {
    data object Ok : ActivateResult
    data class Error(val msg: String) : ActivateResult
}

/**
 * 授权协议 v2：PostgreSQL 统一计算激活/到期时间；客户端只保存并执行服务端结果。
 * 成功复验后可离线使用 24 小时，明确的封禁、解绑、无效卡立即失效。
 */
object LicenseManager {
    private object K {
        val CODE = stringPreferencesKey("code")
        val PLAN = stringPreferencesKey("plan")
        val DAYS = intPreferencesKey("days")
        val EXPIRY = longPreferencesKey("expiry_at")
        val ACTIVATED_AT = longPreferencesKey("activated_at")
        val LAST_VERIFY = longPreferencesKey("last_verify_ms")
        val SERVER_NOW = longPreferencesKey("server_now_ms")
    }

    val PLAN_DAYS = mapOf(
        "monthly" to 30, "quarterly" to 90, "yearly" to 365,
        "lifetime" to 0, "weekly" to 7,
    )

    private const val PUB_KEY_B64 =
        "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEwJd4v53jwXWpinnG1qgVU3D9gM0VvvEFB83Bm/sAEwTcFnISEOJu8DyvyiEW7q9fR9Si7M0afjbMt6k3IOw1hw=="
    private val CODE_RE = Regex("^OTV-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}$")
    private const val DAY_MS = 86_400_000L
    private const val RENEW_BADGE_MS = 7 * DAY_MS
    private const val REVERIFY_INTERVAL_MS = 6 * 3600_000L

    /**
     * 激活请求的 UA（2026-09-16 线上 403 根因修复）。
     *
     * 实测：投影仪直连激活返回「HTTP 403 + Cloudflare 拦截页」，而同一台机器拉比赛列表、
     * 电视源、热更检查（GET）全部正常——差别就在 UA：数据层各接口一律用浏览器 UA
     * （`Mozilla/5.0 … OptimalTV/1.0`），只有激活请求用了 `MyTV/x.y (Android)` 这种
     * 明显的非浏览器标识。Cloudflare 的机器人规则按「出口 IP 信誉 + UA」判定：本机经
     * 代理的好 IP 能过，投影仪的住宅 IP 就被拦成 403 拦截页（请求根本没到我们的服务端，
     * 后台激活日志里一条记录都没有）。
     *
     * 因此激活与数据层统一用浏览器 UA；重试时再换一个更「大众脸」的 Chrome UA，
     * 覆盖边缘规则按具体 UA 判定的情况。
     */
    private const val UA_BROWSER = com.qiubo.optimaltv.data.net.HttpUa.BROWSER
    private const val UA_CHROME =
        "Mozilla/5.0 (Linux; Android 11; SM-G9810) AppleWebKit/537.36 (KHTML, like Gecko) " +
            "Chrome/120.0.0.0 Mobile Safari/537.36"

    private val _state = MutableStateFlow<LicenseState?>(null)
    val state: StateFlow<LicenseState?> = _state.asStateFlow()

    @Volatile private var inited = false
    @Volatile private var lastVerifySuccessAt = 0L
    @Volatile private var serverNowAtVerify = 0L
    private val verifying = AtomicBoolean(false)
    private lateinit var appContext: Context

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val http = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    /**
     * 兜底客户端：只走 HTTP/1.1。部分边缘节点的机器人规则按 HTTP/2 指纹判定，
     * 换协议常常就能过（与换 UA 同一类规避，属激活链路的最后一档）。
     */
    private val http11 = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .protocols(listOf(okhttp3.Protocol.HTTP_1_1))
        .build()

    private val pubKey: PublicKey by lazy {
        KeyFactory.getInstance("EC")
            .generatePublic(X509EncodedKeySpec(Base64.getDecoder().decode(PUB_KEY_B64)))
    }

    fun init(context: Context) {
        if (inited) return
        inited = true
        appContext = context.applicationContext
        scope.launch {
            runCatching {
                val p = appContext.licenseStore.data.first()
                lastVerifySuccessAt = p[K.LAST_VERIFY] ?: 0L
                serverNowAtVerify = p[K.SERVER_NOW] ?: 0L
                val code = p[K.CODE]
                _state.value = if (code.isNullOrBlank()) {
                    LicenseState.NotActivated
                } else {
                    LicenseState.Activated(
                        LicenseActivated(
                            code = code,
                            plan = p[K.PLAN] ?: "monthly",
                            days = p[K.DAYS] ?: 30,
                            expiryAt = p[K.EXPIRY]?.takeIf { it >= 0L },
                            activatedAt = p[K.ACTIVATED_AT] ?: 0L,
                        )
                    )
                }
            }.onFailure {
                OtvLog.w("license load failed: ${it.message}")
                _state.value = LicenseState.NotActivated
            }
            /* 重装自愈（2026-09-16）：卡密只存在本 App 私有数据里，卸载/重装会被系统清空；
               而设备指纹（sha256(ANDROID_ID|包名|机型)）在同一台机器上重装后不变，所以向
               服务端认领回「已绑定的那张卡」。成功后用户无需再输卡密；失败按未激活处理。 */
            if (_state.value is LicenseState.NotActivated) {
                runCatching { tryRestore() }.onFailure { OtvLog.w("license restore error: ${it.message}") }
            }
            silentReverify()
            while (inited) {
                delay(REVERIFY_INTERVAL_MS)
                silentReverify()
            }
        }
    }

    fun isPremium(): Boolean {
        val data = (_state.value as? LicenseState.Activated)?.data ?: return false
        val localNow = System.currentTimeMillis()
        return LicensePolicy.withinOfflineGrace(lastVerifySuccessAt, localNow) &&
            (data.expiryAt == null || effNow() < data.expiryAt)
    }

    fun expiryAt(): Long? = (_state.value as? LicenseState.Activated)?.data?.expiryAt

    fun daysLeft(): Int {
        val st = _state.value as? LicenseState.Activated ?: return 0
        val expiry = st.data.expiryAt ?: return Int.MAX_VALUE
        return ((expiry - effNow()) / DAY_MS).toInt().coerceAtLeast(0)
    }

    fun isLifetime(): Boolean =
        (_state.value as? LicenseState.Activated)?.data?.expiryAt == null

    fun needRenewBadge(): Boolean {
        val expiry = (_state.value as? LicenseState.Activated)?.data?.expiryAt ?: return false
        return effNow() > expiry - RENEW_BADGE_MS
    }

    /** 以最近一次 HTTPS 响应的 serverNow 为基准推进，避免再由客户端推算激活期限。 */
    fun effNow(): Long {
        if (serverNowAtVerify <= 0L || lastVerifySuccessAt <= 0L) return System.currentTimeMillis()
        val elapsed = (System.currentTimeMillis() - lastVerifySuccessAt).coerceAtLeast(0L)
        return serverNowAtVerify + elapsed
    }

    @SuppressLint("HardwareIds")
    fun deviceId(): String {
        val androidId = runCatching {
            Settings.Secure.getString(appContext.contentResolver, Settings.Secure.ANDROID_ID)
        }.getOrNull() ?: "unknown"
        return LicensePolicy.deviceId(androidId, appContext.packageName, Build.MODEL ?: "unknown")
    }

    suspend fun activate(rawCode: String): ActivateResult {
        if (!inited) return ActivateResult.Error("授权模块未初始化")
        val code = normalizeCode(rawCode)
        if (!CODE_RE.matches(code)) return ActivateResult.Error("卡密格式不正确（OTV-XXXXX-XXXXX）")

        val current = (_state.value as? LicenseState.Activated)?.data
        val action = if (current != null && current.code != code) "renew" else "activate"
        val resp = callEndpoint(action, code, deviceId(), current?.code)
            ?: return ActivateResult.Error("网络不可用，请稍后重试")
        /* 响应不是 JSON（网关拦截页 / 5xx / 空响应）：ret 是真实 HTTP 状态，不能按业务码解读，
           否则「403 拦截页」会被显示成「该卡密已被封禁」。 */
        if (resp.msg?.startsWith("bad_json") == true) {
            OtvLog.w("license 激活异常响应：${resp.msg}")
            /* 边缘拦截页（Cloudflare / 运营商劫持）与「网关 5xx」要给用户不同的下一步：
               前者换网络就能过，后者只需重试。截断的响应体里能认出 Cloudflare 特征。 */
            val blocked = resp.msg.contains("Attention Required", true) ||
                resp.msg.contains("Cloudflare", true) ||
                resp.msg.contains("cf-error", true)
            if (blocked) {
                return ActivateResult.Error("激活被网络中间层拦截（HTTP ${resp.ret}），请换网络后重试")
            }
            return ActivateResult.Error("服务器无响应（HTTP ${resp.ret}），请检查网络后重试")
        }
        return when (resp.ret) {
            0 -> if (applySuccess(resp)) ActivateResult.Ok else ActivateResult.Error("服务端票据校验失败（E2）")
            400 -> ActivateResult.Error("卡密格式或请求不正确")
            402 -> ActivateResult.Error("该卡密绑定设备数已满（一码 2 台）")
            403 -> ActivateResult.Error("该卡密已被封禁")
            404 -> ActivateResult.Error("卡密不存在，请检查输入")
            405 -> ActivateResult.Error("当前会员已到期，请使用新卡续费")
            406 -> ActivateResult.Error("当前设备未绑定该卡密")
            409 -> ActivateResult.Error("该续费卡已使用")
            410 -> ActivateResult.Error("终身会员无需续费")
            426 -> ActivateResult.Error("当前版本过旧，请先更新 App")
            else -> ActivateResult.Error("激活失败（${resp.ret}），请稍后重试")
        }
    }

    /** 进入播放门控或回到前台时可触发；节流与并发保护由管理器统一处理。 */
    fun requestReverify() {
        if (!inited || _state.value !is LicenseState.Activated) return
        val due = System.currentTimeMillis() - lastVerifySuccessAt >= REVERIFY_INTERVAL_MS
        if (due) scope.launch { silentReverify() }
    }

    private fun normalizeCode(rawCode: String): String {
        val compact = rawCode.trim().uppercase().filter { it.isLetterOrDigit() }
        return when {
            compact.length == 13 && compact.startsWith("OTV") ->
                "OTV-${compact.substring(3, 8)}-${compact.substring(8)}"
            compact.length == 10 -> "OTV-${compact.substring(0, 5)}-${compact.substring(5)}"
            else -> ""
        }
    }

    /**
     * 设备维度恢复（2026-09-16）：只带设备指纹，请服务端找回「这台设备已经绑定过的那张卡」。
     * 服务端只回已绑定的授权，不新增设备位、不延长有效期；认领不到就返回 false（当作未激活，
     * 正常走激活页）。这样同一台机器重装后用户不必再输一遍卡密。
     */
    private suspend fun tryRestore(): Boolean {
        val resp = callEndpoint("restore", "", deviceId(), null) ?: return false
        if (resp.ret != 0) {
            OtvLog.i("license restore: ret=${resp.ret}（未认领到，按未激活处理）")
            return false
        }
        val ok = applySuccess(resp)
        if (ok) OtvLog.i("license restore ok: plan=${resp.ticket?.plan}")
        return ok
    }

    private suspend fun applySuccess(resp: EndpointResp): Boolean {
        val ticket = resp.ticket ?: return false
        val licenseCode = resp.licenseCode ?: return false
        val activatedAt = resp.activatedAt ?: return false
        val serverNow = resp.serverNow ?: return false
        if (!verifyTicket(ticket, licenseCode)) return false

        val data = LicenseActivated(licenseCode, ticket.plan, ticket.days, resp.expireAt, activatedAt)
        val localNow = System.currentTimeMillis()
        lastVerifySuccessAt = localNow
        serverNowAtVerify = serverNow
        _state.value = LicenseState.Activated(data)
        appContext.licenseStore.edit { p ->
            p[K.CODE] = data.code
            p[K.PLAN] = data.plan
            p[K.DAYS] = data.days
            p[K.EXPIRY] = data.expiryAt ?: -1L
            p[K.ACTIVATED_AT] = data.activatedAt
            p[K.LAST_VERIFY] = localNow
            p[K.SERVER_NOW] = serverNow
        }
        OtvLog.i("license v2 accepted: plan=${ticket.plan}")
        return true
    }

    private suspend fun clearLocal() {
        lastVerifySuccessAt = 0L
        serverNowAtVerify = 0L
        _state.value = LicenseState.NotActivated
        appContext.licenseStore.edit { it.clear() }
    }

    private suspend fun markExpired(expireAt: Long?) {
        val current = (_state.value as? LicenseState.Activated)?.data ?: return
        val effectiveExpiry = expireAt ?: minOf(current.expiryAt ?: effNow(), effNow())
        _state.value = LicenseState.Activated(current.copy(expiryAt = effectiveExpiry))
        appContext.licenseStore.edit { it[K.EXPIRY] = effectiveExpiry }
    }

    private suspend fun silentReverify() {
        val current = (_state.value as? LicenseState.Activated)?.data ?: return
        if (!verifying.compareAndSet(false, true)) return
        try {
            val resp = withTimeoutOrNull(20_000L) {
                callEndpoint("verify", current.code, deviceId(), null)
            } ?: return
            when (resp.ret) {
                0 -> if (!applySuccess(resp)) OtvLog.w("license verify ticket rejected")
                403, 404, 406 -> {
                    OtvLog.w("license rejected by server: ${resp.ret}")
                    clearLocal()
                }
                405 -> markExpired(resp.expireAt)
                426 -> OtvLog.w("license protocol upgrade required")
                else -> Unit // 网络/5xx 不刷新成功时间，满 24 小时后门控自动暂停
            }
        } finally {
            verifying.set(false)
        }
    }

    private data class Ticket(
        val v: Int, val code: String, val plan: String, val days: Int,
        val issued: String, val nonce: String, val sig: String,
    )

    private fun parseTicket(jo: JSONObject): Ticket? = runCatching {
        Ticket(
            v = jo.getInt("v"), code = jo.getString("code"),
            plan = jo.getString("plan"), days = jo.getInt("days"),
            issued = jo.getString("issued"), nonce = jo.getString("nonce"),
            sig = jo.getString("sig"),
        )
    }.getOrNull()

    private fun verifyTicket(ticket: Ticket, canonicalCode: String): Boolean {
        if (ticket.v != 1 || ticket.code != canonicalCode || PLAN_DAYS[ticket.plan] != ticket.days) return false
        if (ticket.issued.length != 10 || ticket.nonce.length < 8 || ticket.sig.length < 64) return false
        val canonical = "1|${ticket.code}|${ticket.plan}|${ticket.days}|${ticket.issued}|${ticket.nonce}"
        return runCatching {
            val verifier = Signature.getInstance("SHA256withECDSA")
            verifier.initVerify(pubKey)
            verifier.update(canonical.toByteArray(Charsets.UTF_8))
            verifier.verify(Base64.getDecoder().decode(ticket.sig))
        }.onFailure { OtvLog.w("ticket verify error: ${it.message}") }.getOrDefault(false)
    }

    private data class EndpointResp(
        val ret: Int,
        val ticket: Ticket?,
        val msg: String?,
        val licenseCode: String?,
        val activatedAt: Long?,
        val expireAt: Long?,
        val serverNow: Long?,
    )

    private suspend fun callEndpoint(
        action: String,
        code: String,
        deviceId: String,
        currentCode: String?,
    ): EndpointResp? = withContext(Dispatchers.IO) {
        val payload = JSONObject()
            .put("protocol", 2)
            .put("action", action)
            .put("code", code)
            .put("deviceId", deviceId)
        if (action == "renew") payload.put("currentCode", currentCode)

        fun once(client: okhttp3.OkHttpClient, ua: String): EndpointResp? = runCatching {
            val request = Request.Builder()
                .url(BuildConfig.LICENSE_ENDPOINT)
                // 浏览器 UA + 常规请求头：见 UA_BROWSER 注释（非浏览器 UA 会被边缘拦成 403）
                .header("User-Agent", ua)
                .header("Accept", com.qiubo.optimaltv.data.net.HttpUa.ACCEPT)
                .header("Accept-Language", com.qiubo.optimaltv.data.net.HttpUa.ACCEPT_LANGUAGE)
                .post(payload.toString().toRequestBody("application/json".toMediaType()))
                .build()
            client.newCall(request).execute().use { response ->
                val raw = response.body?.string().orEmpty()
                val jo = runCatching { JSONObject(raw) }.getOrNull()
                    /* 响应不是 JSON（网关 5xx / 拦截页 / 空响应）：保留真实 HTTP 状态和一小段
                       响应体，否则底层统一成 502 会把「403 拦截页」误读成「该卡密已被封禁」。 */
                    ?: return@use EndpointResp(
                        ret = response.code.takeIf { it >= 400 } ?: 502, ticket = null,
                        msg = "bad_json:${response.code}:" + raw.take(80).replace('\n', ' '),
                        licenseCode = null, activatedAt = null, expireAt = null, serverNow = null,
                    )
                EndpointResp(
                    ret = jo.optInt("ret", if (response.isSuccessful) 500 else response.code),
                    ticket = jo.optJSONObject("ticket")?.let(::parseTicket),
                    msg = jo.optString("msg").takeIf { it.isNotBlank() },
                    licenseCode = jo.optString("licenseCode").takeIf { it.isNotBlank() },
                    activatedAt = parseTimestamp(jo.opt("activatedAt")),
                    expireAt = parseTimestamp(jo.opt("expireAt")),
                    serverNow = parseTimestamp(jo.opt("serverNow")),
                )
            }
        }.onFailure { OtvLog.w("license endpoint unavailable: ${it.message}") }.getOrNull()

        /* 网关偶发 5xx / 拦截页 / 连接抖动：原地重试一次。激活本身幂等——同一设备重复激活
           只会回同一张票、不新增设备位，所以重试是安全的。 */
        /* 三档尝试（激活幂等：同设备重复激活只回同一张票、不占设备位）：
           ① 数据层同款浏览器 UA；② 大众脸 Chrome UA；③ 再叠一层「只走 HTTP/1.1」。
           只有「无响应 / 5xx / 非 JSON 拦截页」才继续下一档——业务码（卡密不存在、设备数满等）
           是真实结论，不重试。 */
        val attempts = listOf(
            Triple(http, UA_BROWSER, "浏览器UA"),
            Triple(http, UA_CHROME, "ChromeUA"),
            Triple(http11, UA_CHROME, "ChromeUA+HTTP/1.1"),
        )
        var last: EndpointResp? = null
        for ((client, ua, label) in attempts) {
            val resp = once(client, ua)
            if (resp == null) {
                OtvLog.i("license endpoint 无响应（$label）→ 尝试下一档")
                continue
            }
            last = resp
            val retryable = resp.ret >= 500 || resp.msg?.startsWith("bad_json") == true
            if (!retryable) return@withContext resp
            OtvLog.i("license endpoint 异常（$label ret=${resp.ret} ${resp.msg?.take(40)}）→ 尝试下一档")
        }
        last
    }

    /* 2026-09-19 修复 E2（激活成功但客户端拒票）：PG 返回的 activatedAt/serverNow 是
       带本地时区偏移的 ISO 串（如 2026-09-19T00:21:34.253846+08:00），Instant.parse 只认
       UTC（Z 结尾），偏移时间戳直接抛异常 → 字段变 null → applySuccess 误报
       「服务端票据校验失败（E2）」。补 OffsetDateTime 兜底解析带偏移的形态。 */
    private fun parseTimestamp(value: Any?): Long? = when (value) {
        is Number -> value.toLong()
        is String -> value.toLongOrNull()
            ?: runCatching { Instant.parse(value).toEpochMilli() }.getOrNull()
            ?: runCatching { OffsetDateTime.parse(value).toInstant().toEpochMilli() }.getOrNull()
        else -> null
    }
}

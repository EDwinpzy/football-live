package com.qiubo.optimaltv.data.repo

import android.content.Context
import android.os.SystemClock
import com.qiubo.optimaltv.BuildConfig
import com.qiubo.optimaltv.OtvLog
import com.qiubo.optimaltv.data.source.UrlGuard
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * IPTV catalog: checked built-in fallbacks plus the admin-managed channel list.
 * Refreshes run on an IO thread. Stream probes are bounded and never block UI input.
 * Upstream availability varies by network; the player can rotate through channel fallbacks.
 */
class IptvRepository(context: Context) {

    companion object {
        /** 上游补源地址按序回退（命中即停）。2026-10-12 两轮实测重排：
         *  第一轮 artifacts/可用源清单-2026-10-12.md（d1y/kitty lives.json 的 4 条），
         *  第二轮 github.com/ngo5/IPTV 清单（新增 7 条，实测均 200 且正文是合法 m3u）。
         *  ① 站点每日校验推送产物（/iptv/refresh 定时拉上游→逐线测活剔死→按测速排序
         *     写入对象存储，匿名 GET 直拉——电视#9「每天自动推送可用源、删除不可用源」；
         *     对象与热更包同桶不同前缀 iptv/）。
         *  ②~⑪ 上游 m3u，顺序 = 覆盖广度优先（与 proxy.py IPTV_SOURCES、后台
         *     IPTV_UPSTREAMS 同序）。旧的 JianghaoPi / iptv-org 清单与 gh-proxy 前缀
         *     已全部移除（gh-proxy 同一 URL 前后两次结果可能不同，200 ↔ 404）。
         *  2026-10-13 复核：ngo5/IPTV 的 m3u/ipv4.m3u 已在上游链中（158 频道）；
         *  YueChan/Live IPTV.m3u 虽有 96 条，但全是 rtp:// 组播，
         *  客户端 UrlGuard 只接受 http/https，已剔除。
         *  其他实测不合格、不要再加回来：tv.iill.top/m3u/Gather（HTML）、
         *  live.zbds.org（000）、live.fanmingming.cn（000）、82.156.243.185:54321（000）、
         *  joevess/IPTV（正文 8 字节）、Ftindy/IPTV-URL（404）、aktv.space（302 到 HTML）；
         *  纯 IPv6 清单（Guovin ipv6 / myIPTV ipv6 / m3u.ibert.me / ngo5 ipv6Plus）不收。 */
        val SOURCES = listOf(
                // 由 apiBaseUrl Gradle 属性注入：迁移后指向新服务器，旧 CloudBase 地址不再内置。
                BuildConfig.IPTV_LATEST_URL,
            "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
            "https://raw.githubusercontent.com/suxuang/myIPTV/refs/heads/main/ipv4.m3u",
            "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv4/result.m3u",
            "https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u",
            "https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/cnTV_AutoUpdate.m3u8",
            "https://raw.githubusercontent.com/mytv-android/China-TV-Live-M3U8/main/iptv.m3u",
            "https://raw.githubusercontent.com/ngo5/IPTV/main/m3u/ipv4.m3u",
            "https://raw.githubusercontent.com/BurningC4/Chinese-IPTV/master/TV-IPV4.m3u",
            "https://raw.githubusercontent.com/zwc456baby/iptv_alive/refs/heads/master/live.m3u",
            "https://raw.githubusercontent.com/kimwang1978/collect-tv-txt/main/bbxx_lite.m3u",
        )
        /** 兜底链总预算与单条上限（见 fetchLatest 注释）。 */
        private const val FETCH_TOTAL_BUDGET_MS = 45_000L
        private const val FETCH_PER_SOURCE_MS = 20_000L
        private const val CURATED_ASSET = "iptv_curated.m3u"
        private const val LOGO_INDEX = "logos/index.json"
        private const val SNAPSHOT = "iptv_snapshot.m3u"
        private const val STALE_MS = 30L * 60 * 1000   // 自动刷新节流阈值
        /** 单频道线路数上限（精编线在前 + 上游补源线殿后，超出丢弃） */
        private const val MAX_LINES_PER_CHANNEL = 8

        /** 分组展示顺序 = 精编表分组（未知分组按名序殿后） */
        private val GROUP_ORDER = listOf(
            "央视", "卫视", "体育", "地方", "数字付费", "港澳台", "美国/英国",
        )

        /**
         * 频道名归一化（补源匹配用）：小写、去空白、全角+转半角；
         * CCTV/CGTN 前缀频道剥掉数字后的中文后缀（CCTV-13新闻≡CCTV13、CCTV-5+体育赛事≡CCTV5+，
         * 「+」保留防 CCTV5+ 误并入 CCTV5）；其余频道（东方卫视/凤凰中文台/NewTV超级电影…）取全名。
         */
        fun normKey(name: String): String {
            var n = name.trim().lowercase()
                .replace("＋", "+").replace("－", "-").replace("\\s+".toRegex(), "")
            val m = Regex("^(cctv|cgtn)[-–—]?(\\d+)(\\+?)").find(n) ?: return n
            return m.groupValues[1] + m.groupValues[2] + m.groupValues[3]
        }

    }

    /**
     * 频道：一个频道名 = 一行侧边栏条目，[urls] 为主线路+备用线路（主线路在前）。
     * [url] 兼容旧单线路用法（= 主线路）。
     */
    data class Channel(
        val name: String,
        val group: String,
        val logo: String,
        val urls: List<String>,
    ) {
        val url: String get() = urls.first()
    }

    data class IptvState(
        val loading: Boolean = true,
        /** 解析后被安全策略剔除的线路数（验收日志用） */
        val blockedCount: Int = 0,
        val blockedSamples: List<String> = emptyList(),
        val channels: List<Channel> = emptyList(),
        val groups: List<String> = emptyList(),
        /** 最近一次拉取：来源（成功）/错误（失败）描述 */
        val lastResult: String? = null,
        val refreshedAtMs: Long = 0L,
    )

    private val appCtx = context.applicationContext
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val _state = MutableStateFlow(IptvState())
    val state: StateFlow<IptvState> = _state.asStateFlow()

    @Volatile private var refreshing = false
    @Volatile private var lastSuccessAt = 0L
    /** v1.17 拉取失败自动重试：60s 起指数退避（上限 10min），成功后复位；进程内单线程 */
    @Volatile private var retryDelayMs = 60_000L
    @Volatile private var retryScheduled = false

    /** m3u 解析中间结构：按 (分组, 频道名) 聚合线路，保持首次出现顺序 */
    private class RawChannel(
        val name: String, val group: String,
        var logo: String = "",
        val urls: ArrayList<String> = ArrayList(),
    )

    /** 解析产物：频道列表 + 被安全策略剔除的线路计数/样本 */
    private class Parsed(
        val channels: List<RawChannel>,
        val blocked: Int,
        val samples: List<String>,
    )

    init {
        // 冷启动：快照先上屏（秒开）；快照缺失/损坏 → 内置精编表兜底（离线也有完整名单）。
        // v1.19 主线程减负：读取+解析挪后台 IO（旧版在 onCreate 主线程同步读盘+正则
        // 解析+台标索引加载，低端机冷启动白屏被拉长）；上屏守卫与目录快照同款——
        // 仅当内存还没有频道（refresh 未抢先完成）才赋值，不覆盖新数据。
        kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO).launch {
            runCatching {
                val snap = runCatching { snapshotFile().takeIf { it.exists() }?.readText() }.getOrNull()
                val restored = snap?.let { parseM3u(it) }?.takeIf { it.channels.isNotEmpty() }
                if (_state.value.channels.isEmpty()) {
                    if (restored != null) {
                        _state.value = buildState(restored, "磁盘快照（后台刷新中）")
                        OtvLog.i("iptv 快照恢复上屏 ${restored.channels.size} 频道")
                    } else {
                        val curated = parseM3u(curatedAssetText())
                        _state.value = buildState(curated, "内置精编频道表")
                        OtvLog.i("iptv 内置精编表上屏 ${curated.channels.size} 频道（快照缺失，后台拉取补源）")
                    }
                }
            }
        }
    }

    private fun snapshotFile(): File = File(appCtx.filesDir, SNAPSHOT)

    private fun curatedAssetText(): String = runCatching {
        appCtx.assets.open(CURATED_ASSET).bufferedReader().use { it.readText() }
    }.getOrDefault("")

    /**
     * v1.17 内置真实台标表（assets/logos/index.json，构建期由 tools/build_logo_assets.py
     * 打包全部频道的真实台标 PNG）：normKey 或频道名 → 资产文件名。命中即优先生效
     * （file:///android_asset 离线可用、不依赖任何 CDN），未命中回退上游 tvg-logo。
     */
    private var logoAssetsCache: Map<String, String>? = null

    private fun logoAssets(): Map<String, String> {
        logoAssetsCache?.let { return it }
        val m = runCatching {
            val o = org.json.JSONObject(
                appCtx.assets.open(LOGO_INDEX).bufferedReader().use { it.readText() },
            )
            buildMap { o.keys().forEach { k -> put(k, o.getString(k)) } }
        }.getOrDefault(emptyMap())
        logoAssetsCache = m
        return m
    }

    private fun assetLogoFor(name: String): String? =
        (logoAssets()[name] ?: logoAssets()[normKey(name)])?.let { "file:///android_asset/logos/$it" }

    /** 进页/启动触发：节流 + 去重（refreshing 期间不叠加） */
    fun refreshIfStale(force: Boolean = false) {
        if (!force && System.currentTimeMillis() - lastSuccessAt < STALE_MS) return
        refresh()
    }

    fun refresh() {
        if (refreshing) return
        refreshing = true
        val wasEmpty = _state.value.channels.isEmpty()
        if (wasEmpty) _state.value = _state.value.copy(loading = true)
        Thread {
            try {
                fetchLatest()?.let { fetched ->
                    val upstreamRaw = fetched.raw
                    // v1.20 电视源管理（后台#8）托管模式：云端 m3u 带非空分组（group-title）
                    // = 后台手动发布的托管列表 → 整表直接生效（频道/分组/顺序以后台为准，
                    // 后台可动态增删频道）；自动推流产物分组为空 → 维持旧「精编名单+线路池」
                    // 合并语义（精编表定名单，上游只补线路）
                    val curated = parseM3u(curatedAssetText())
                    val upstream = parseM3u(upstreamRaw)
                    val managed = fetched.url == BuildConfig.IPTV_LATEST_URL &&
                        upstream.channels.any { it.group.isNotBlank() && it.group != "其他" }
                    val merged = if (managed) upstream else merge(curated, upstream)
                    require(merged.channels.isNotEmpty()) { "合并结果为空" }
                    val raw = serializeM3u(merged.channels)
                    // 快照原子落盘（先写临时文件再改名，读侧永不读到半截）
                    runCatching {
                        val tmp = File(appCtx.filesDir, "$SNAPSHOT.tmp")
                        tmp.writeText(raw)
                        if (!tmp.renameTo(snapshotFile())) {
                            snapshotFile().delete(); tmp.renameTo(snapshotFile())
                        }
                    }
                    lastSuccessAt = System.currentTimeMillis()
                    retryDelayMs = 60_000L   // 成功复位退避
                    _state.value = buildState(merged, "已更新（补源 ${upstreamRaw.length / 1024}KB）")
                    val sup = merged.channels.sumOf { (it.urls.size - 1).coerceAtLeast(0) }
                    OtvLog.i("iptv 拉取+补源成功：${merged.channels.size} 频道 / 备用线路 $sup 条" +
                        "（安全剔除 ${merged.blocked}）")
                } ?: throw IllegalStateException("全部源不可达：${SOURCES.size} 个地址均失败")
            } catch (e: Exception) {
                OtvLog.w("iptv 拉取失败: ${e.message}（保留旧列表，${retryDelayMs / 1000}s 后自动重试）")
                _state.value = _state.value.copy(
                    loading = false,
                    lastResult = if (_state.value.channels.isEmpty()) "直播源加载失败：${e.message}"
                    else "刷新失败（已保留 ${_state.value.channels.size} 个频道）",
                )
                scheduleAutoRetry()
            } finally {
                refreshing = false
            }
        }.apply { isDaemon = true; name = "otv-iptv-refresh" }.start()
    }

    /** 源失效自动重试（v1.17）：失败后 60s→120s→…→10min 退避重拉，成功复位 */
    private fun scheduleAutoRetry() {
        if (retryScheduled) return
        retryScheduled = true
        val delay = retryDelayMs
        retryDelayMs = (retryDelayMs * 2).coerceAtMost(600_000L)
        Thread {
            try { Thread.sleep(delay) } catch (_: InterruptedException) {}
            retryScheduled = false
            OtvLog.i("iptv 自动重试拉取（退避 ${delay / 1000}s 后）")
            refresh()
        }.apply { isDaemon = true; name = "otv-iptv-retry" }.start()
    }

    /** 多源按序拉取上游原始 m3u 文本；全部失败返回 null */
    private data class FetchedPlaylist(val url: String, val raw: String)

    private fun fetchLatest(): FetchedPlaylist? {
        // 兜底链总预算（2026-10-13）：SOURCES 从 5 条涨到 11 条（latest + 10 个备用），而本机 client 是
        // connect 15s + read 30s，逐条硬等意味着「全部源不可达」最坏要 8 分钟。
        // 整链共享一个 45s 硬顶，单条请求超时取「剩余预算」；站点托管 latest 命中时
        // 仍是一次请求，正常路径行为不变。
        val deadline = System.nanoTime() + FETCH_TOTAL_BUDGET_MS * 1_000_000L
        for (src in SOURCES) {
            // 出站前过 UrlGuard（多源地址为内置 https 公网域，防未来配置笔误带入内网地址）
            val verdict = UrlGuard.check(src)
            if (verdict.blocked) {
                OtvLog.w("iptv 源地址被 UrlGuard 拒绝：$src（${verdict.reason}）")
                continue
            }
            val remainingMs = (deadline - System.nanoTime()) / 1_000_000L
            if (remainingMs <= 200L) {
                OtvLog.w("iptv 兜底链预算耗尽，放弃剩余 ${SOURCES.size} 条上游中的后续地址")
                break
            }
            try {
                val call = client.newCall(Request.Builder().url(src).build())
                call.timeout().timeout(minOf(remainingMs, FETCH_PER_SOURCE_MS), TimeUnit.MILLISECONDS)
                call.execute().use { resp ->
                    if (!resp.isSuccessful) {
                        OtvLog.w("iptv 源 $src HTTP ${resp.code}")
                        return@use
                    }
                    val body = resp.body?.string()
                    if (!body.isNullOrBlank() && body.contains("#EXTINF")) {
                        OtvLog.i("iptv 源命中：$src（${body.length} 字符）")
                        return FetchedPlaylist(src, body)
                    }
                }
            } catch (e: Exception) {
                OtvLog.w("iptv 源 $src 拉取异常：${e.message}")
            }
        }
        return null
    }

    /**
     * m3u 解析（线路级）：#EXTINF 行 + 次行 URL；每个 URL 过 UrlGuard，被拒线路剔除并记录样本。
     * 兼容名称/URL 单行 txt 风格兜底。同名同 URL 只保留一条。
     */
    private fun parseM3u(raw: String): Parsed {
        val order = ArrayList<RawChannel>(128)
        val byKey = HashMap<String, RawChannel>(256)
        var blocked = 0
        val samples = ArrayList<String>(4)
        var pendingName = ""; var pendingLogo = ""; var pendingGroup = ""

        fun offer(name: String, url: String, logo: String, group: String) {
            if (name.isBlank() || url.isBlank()) return
            val v = UrlGuard.check(url)
            if (v.blocked) {
                blocked++
                if (samples.size < 4) samples.add("$name → $url（${v.reason}）")
                return
            }
            val key = group.trim() + "|" + name.trim()
            val ch = byKey.getOrPut(key) {
                RawChannel(name.trim(), group.trim().ifBlank { "其他" }).also { order.add(it) }
            }
            if (ch.logo.isBlank()) ch.logo = logo.trim()
            if (ch.urls.size < MAX_LINES_PER_CHANNEL && url.trim() !in ch.urls) ch.urls.add(url.trim())
        }

        raw.lineSequence().forEach { rawLine ->
            val line = rawLine.trim()
            if (line.startsWith("#EXTINF")) {
                pendingName = line.substringAfterLast(',').trim()
                pendingLogo = Regex("tvg-logo=\"([^\"]*)\"").find(line)?.groupValues?.get(1).orEmpty()
                pendingGroup = Regex("group-title=\"([^\"]*)\"").find(line)?.groupValues?.get(1).orEmpty()
            } else if (line.startsWith("http")) {
                offer(pendingName, line, pendingLogo, pendingGroup)
                pendingName = ""; pendingLogo = ""; pendingGroup = ""
            } else if (!line.startsWith("#") && line.contains(',')) {
                // txt 兜底：名称,URL
                val idx = line.indexOf(',')
                offer(line.substring(0, idx), line.substring(idx + 1), "", "")
            }
        }
        return Parsed(order.filter { it.urls.isNotEmpty() }, blocked, samples)
    }

    /**
     * 精编名单 × 上游补源合并：以精编表频道为名单（顺序即精编表顺序），上游按归一化名
     * 匹配（名称 + tvg-name 双 key），追加主线路之外的备用线路与台标；上游独有频道丢弃。
     */
    private fun merge(curated: Parsed, upstream: Parsed): Parsed {
        val byNorm = HashMap<String, RawChannel>(upstream.channels.size * 2)
        upstream.channels.forEach { ch ->
            byNorm.putIfAbsent(normKey(ch.name), ch)
        }
        curated.channels.forEach { cur ->
            val sup = byNorm[normKey(cur.name)] ?: return@forEach
            if (cur.logo.isBlank()) cur.logo = sup.logo
            sup.urls.forEach { u ->
                if (cur.urls.size >= MAX_LINES_PER_CHANNEL) return@forEach
                if (u !in cur.urls) cur.urls.add(u)
            }
        }
        return Parsed(curated.channels, curated.blocked + upstream.blocked, curated.samples)
    }

    /** 解析产物 → 上屏状态：分组排序（预定义顺序，未知殿后按名序）+ 频道名自然序 */
    private fun buildState(list: Parsed, result: String): IptvState {
        val channels = list.channels.map {
            Channel(it.name, it.group, assetLogoFor(it.name) ?: it.logo, it.urls.toList())
        }
        val groups = channels.map { it.group }.distinct().sortedWith(
            compareBy<String> { g -> GROUP_ORDER.indexOf(g).let { if (it < 0) GROUP_ORDER.size + 1 else it } }
                .thenBy { it },
        )
        val sorted = channels.sortedWith(
            compareBy<Channel> { c -> GROUP_ORDER.indexOf(c.group).let { if (it < 0) GROUP_ORDER.size + 1 else it } }
                .thenBy { it.group }.thenBy { channelNameKey(it.name) },
        )
        return IptvState(
            loading = false, channels = sorted, groups = groups, lastResult = result,
            blockedCount = list.blocked, blockedSamples = list.samples,
            refreshedAtMs = if (result.startsWith("已更新")) System.currentTimeMillis() else 0L,
        )
    }

    /** 频道名自然序 key：CCTV1/CCTV5+/CCTV13 按数字排（CCTV1 < CCTV5+ < CCTV13 < CCTV怀旧…） */
    private fun channelNameKey(name: String): String {
        val m = Regex("^(CCTV|CGTN)?[- ]*(\\d+)(.*)$").find(name.uppercase()) ?: return name
        val (prefix, num, rest) = m.destructured
        return "${prefix.ifBlank { "~" }}%04d".format(num.toIntOrNull() ?: 9999) + rest
    }

    /** 合并结果序列化回 m3u（快照持久化格式；解析器可原样读回） */
    private fun serializeM3u(list: List<RawChannel>): String = buildString {
        appendLine("#EXTM3U")
        list.forEach { ch ->
            ch.urls.forEach { u ->
                appendLine("#EXTINF:-1 tvg-logo=\"${ch.logo}\" group-title=\"${ch.group}\",${ch.name}")
                appendLine(u)
            }
        }
    }

    suspend fun awaitReady() {
        // 外部（debug 直达通道）需要列表就绪时的轮询等待
        withContext(Dispatchers.IO) {
            var guard = 0
            while (_state.value.channels.isEmpty() && guard++ < 100) {
                kotlinx.coroutines.delay(200)
            }
        }
    }

    /* ================= 线路实时测速（电视#5：优先选最快、随时切换） ================= */

    /** 已测线路的排序视图：成功的按分数升序在前，未测的保持原相对顺序居后，
     *  已证死（FAIL_MS）的垫底兜底。换台时主线路即取首个。
     *  P2 修复（2026-09-04）：旧版把失败线也算 measured 排在未测线之前——
     *  一条已探测失败的死线会压过从未测过（可能健康）的线成为换台首选 */
    fun rankUrls(urls: List<String>): List<String> {
        if (urls.size < 2) return urls
        val scored = urls.mapNotNull { u -> speedOf(u)?.let { u to it } }
        if (scored.isEmpty()) return urls   // 至少两条有分数才值得重排
        val good = scored.filter { it.second < LineSpeed.FAIL_MS }.sortedBy { it.second }
        val dead = scored.filter { it.second >= LineSpeed.FAIL_MS }
        return good.map { it.first } +
            urls.filter { speedOf(it) == null } +
            dead.map { it.first }
    }

    fun speedOf(url: String): Long? = LineSpeed.scoreOf(url)

    /** 并发测一组的全部线路（电视页换台/驻留期轮询用）；去重防叠加。 */
    suspend fun probeAll(urls: List<String>) {
        urls.distinct().filter { !LineSpeed.isProbing(it) }.let { fresh ->
            if (fresh.isEmpty()) return
            kotlinx.coroutines.coroutineScope {
                fresh.forEach { u ->
                    launch(Dispatchers.IO) { LineSpeed.probe(u) }
                }
            }
        }
    }
}

/** 线路测速器（电视#5，IptvRepository 类外顶层——跨模块按名引用） */
/**
 * 线路测速器（电视#5）：对单条播放 URL 检查清单与首个媒体分片，再按总耗时评分；
 * 200 状态的 HTML 错误页、空清单或坏分片都算失败。失败记 [FAIL_MS]。
 * 分数进程内共享（换台排序 ↔ 播放链路兜底互认）；同一 URL 并发探测合并为一次。
 */
object LineSpeed {
    const val FAIL_MS = 999_999L
    private const val READ_TARGET = IptvProbePolicy.MAX_PLAYLIST_BYTES + 1

    private val probeSlots = Semaphore(4)
    private val scores = ConcurrentHashMap<String, Long>()
    private val probing = ConcurrentHashMap.newKeySet<String>()

    private val client = OkHttpClient.Builder()
        .connectTimeout(2, TimeUnit.SECONDS)
        .readTimeout(3, TimeUnit.SECONDS)
        .callTimeout(5, TimeUnit.SECONDS)
        .followRedirects(true)
        .followSslRedirects(true)
        .build()

    fun scoreOf(url: String): Long? = scores[url]

    fun isProbing(url: String): Boolean = url in probing

    /** 探测一条线路并记录分数；失败记 FAIL_MS。返回分数。 */
    suspend fun probe(url: String): Long? = withContext(Dispatchers.IO) {
        if (!probing.add(url)) return@withContext scores[url]
        try {
            val t0 = SystemClock.elapsedRealtime()
            val valid = probeSlots.withPermit { runCatching { inspect(url, 0) }.getOrDefault(false) }
            val ms = if (valid) SystemClock.elapsedRealtime() - t0 else FAIL_MS
            scores[url] = ms
            ms
        } finally {
            probing.remove(url)
        }
    }

    /** 最多跟随两级 HLS 清单（主清单→媒体清单）后检查首个分片。 */
    private fun inspect(url: String, playlistDepth: Int): Boolean {
        val result = client.newCall(
            Request.Builder().url(url)
                .header("User-Agent", "Mozilla/5.0 OptimalTV/probe")
                .header("Range", "bytes=0-8191")
                .build(),
        ).execute().use { response ->
            if (response.code != 200 && response.code != 206) return false
            val body = response.body?.byteStream()?.let { readLimited(it, READ_TARGET) } ?: return false
            ProbeResponse(
                body = body,
                contentType = response.header("Content-Type"),
                finalUrl = response.request.url,
                status = response.code,
                contentRange = response.header("Content-Range"),
            )
        }
        if (IptvProbePolicy.isPlaylist(result.body, result.contentType, url)) {
            if (!IptvProbePolicy.isCompletePlaylistResponse(result.body, result.status, result.contentRange)) return false
            if (!IptvProbePolicy.isLivePlaylist(result.body)) return false
            if (playlistDepth >= 2) return false
            val reference = IptvProbePolicy.firstPlaylistReference(result.body) ?: return false
            val next = result.finalUrl.resolve(reference) ?: return false
            return inspect(next.toString(), playlistDepth + 1)
        }
        if (IptvProbePolicy.isPlaylistUrl(url) || IptvProbePolicy.isPlaylistContentType(result.contentType)) return false
        return IptvProbePolicy.isUsableMediaPayload(result.body, result.contentType)
    }

    private data class ProbeResponse(
        val body: ByteArray,
        val contentType: String?,
        val finalUrl: okhttp3.HttpUrl,
        val status: Int,
        val contentRange: String?,
    )

    private fun readLimited(input: java.io.InputStream, limit: Int): ByteArray {
        val output = java.io.ByteArrayOutputStream(limit)
        val buffer = ByteArray(4096)
        var total = 0
        while (total < limit) {
            val count = input.read(buffer, 0, minOf(buffer.size, limit - total))
            if (count <= 0) break
            output.write(buffer, 0, count)
            total += count
        }
        return output.toByteArray()
    }
}

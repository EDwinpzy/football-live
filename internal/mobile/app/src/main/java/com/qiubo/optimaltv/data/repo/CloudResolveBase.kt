package com.qiubo.optimaltv.data.repo

import com.qiubo.optimaltv.BuildConfig
import com.qiubo.optimaltv.data.prefs.AppSettings

/**
 * 云端片源解析（L1）该打哪个地址。
 *
 * 修复背景（2026-09-29 实测）：点播放到出画 78.8 秒。VodRepository 用
 * `settings.hhkanBaseUrl` 调 fetchResolve，而它的默认值是
 * `http://127.0.0.1:8090` —— App 自己的内嵌后端。于是每次播放的 L1
 * 云端预匹配都打不到服务端，直接回落 local_detail，用本地实时匹配返回的
 * 原始线路顺序：12 条线路逐条等满 5s 首帧看门狗才换线。
 *
 * 服务端能力其实是好的：`/vod/resolve/10000020?episode=1` 返回
 * `MATCHED`，带完整 preferred（猫眼，match_score 100）、cache=hot_cache。
 * 修复分三步：
 *   1. 线路按 LineHistory 健康度排序（服务端 vod_api.detail）；
 *   2. 云端 resolve 打到真正有索引和 resolver 的服务端（这里）；
 *   3. 内嵌后端仍作为兜底，云端不可用时行为不变。
 *
 * 选地址规则：
 *   - Release 注入了 apiBaseUrl（正式域名或 IP 入口）→ 用它，那是服务端；
 *   - 用户手动改过 hhkanBaseUrl 且指向远端 → 尊重用户设置；
 *   - 都没有 → 退回内嵌后端，保持离线可用。
 */
object CloudResolveBase {

    private val LOOPBACK_HOSTS = setOf("127.0.0.1", "localhost", "::1", "0.0.0.0")

    private fun normalize(url: String): String = url.trim().trimEnd('/')

    /** 该地址是否指向远端（不是本机回环）。可见性放宽到 internal 供单测直接覆盖，
     * 因为 Debug 构建的 BuildConfig 总是有值，pick() 的回环分支在单测里被遮住。 */
    internal fun isRemote(url: String): Boolean {
        val normalized = normalize(url)
        if (normalized.isEmpty()) return false
        val afterScheme = normalized.substringAfter("://", normalized)
        // IPv6 字面量带方括号（http://[::1]:8090），要先剥括号再判断。
        val authority = afterScheme.substringBefore('/').substringBefore('?')
        val host = authority.removePrefix("[").substringBefore(']')
            .ifEmpty { authority.removePrefix("[") }
            .substringBefore(':')
        return host.isNotEmpty() && host.lowercase() !in LOOPBACK_HOSTS
    }

    /**
     * 返回给 `/vod/resolve` 用的 base，末尾不带斜杠。
     *
     * 注意前缀：VPS 的 nginx 只把 web 前缀转给 web-api，站点根下的
     * vod 路由是 404（实测）。Release 注入的 apiBaseUrl 正是站点根，
     * 所以远端地址要补上 web 段才是对的落点。
     */
    fun pick(settings: AppSettings): String {
        val api = normalize(settings.apiBaseUrl)
        if (isRemote(api)) return withWebPrefix(api)
        // Release 在构建期注入了已验收的入口（正式域名，或显式 opt-in 的 IP）。
        // DataStore 里的 apiBaseUrl 默认是空串、启动时也没有任何代码写入它，
        // 只看设置项会让这一路永远为空 → 回落内嵌后端。构建期常量才是可靠来源。
        val buildTime = normalize(BuildConfig.API_BASE_URL)
        if (isRemote(buildTime)) return withWebPrefix(buildTime)
        val hhkan = normalize(settings.hhkanBaseUrl)
        if (isRemote(hhkan)) return hhkan
        return if (hhkan.isNotEmpty()) hhkan else buildTime.ifEmpty { api }
    }

    /**
     * 远端站点根补上 `/web`；已经是 `/web` 或带自定义路径的地址不动。
     * 内嵌后端（127.0.0.1:8090）本身就直接暴露 vod 路由，不能加前缀。
     */
    private fun withWebPrefix(base: String): String =
        if (base.endsWith("/web")) base else "$base/web"
}

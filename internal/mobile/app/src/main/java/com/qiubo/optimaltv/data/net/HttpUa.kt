package com.qiubo.optimaltv.data.net

/**
 * 对「自有站点接口」统一的请求 UA（2026-09-16）。
 *
 * 背景：数据层各接口一直用这个浏览器 UA，而激活 / 热更检查 / 公告三条链路用的是
 * OkHttp 默认 UA 或 `MyTV/x.y (Android)`——实测投影仪直连时，激活 POST 被 Cloudflare
 * 拦成「HTTP 403 + Attention Required 拦截页」，请求根本没到服务端（后台激活日志无记录），
 * 而同一台设备拉比赛/电视源（浏览器 UA）一切正常。边缘的机器人规则按「出口 IP 信誉 + UA」
 * 判定，因此这三条链路统一改成与数据层一致的浏览器 UA；激活另有 Chrome UA 与 HTTP/1.1
 * 两档兜底重试。
 */
object HttpUa {
    const val BROWSER = "Mozilla/5.0 (Linux; Android 11) OptimalTV/1.0"
    const val ACCEPT = "application/json, text/plain, */*"
    const val ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9"
}

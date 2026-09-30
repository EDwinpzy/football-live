package com.qiubo.optimaltv.apkupdate

import kotlin.math.roundToInt

/**
 * APK 下载进度文案（2026-10-01）。
 *
 * 真机实测反馈：更新弹窗长期停在「正在下载 0%」。原因是弱网下 OkHttp 在
 * `readTimeout` 内一个字节都收不到，而进度只在**收到字节后**才更新，
 * 于是界面从 0% 起就完全静止——用户分不清是卡死还是在慢慢下。
 *
 * 这个对象只负责把 (已下载, 总量, 速度, 重试次数) 变成一句人话：
 * - 一个字节都没收到时明确说「正在连接服务器」，不再假装在下载 0%；
 * - 开始传输后给出「已下载 / 总量 + 百分比 + 瞬时速度」，停滞会被用户看见；
 * - 速度按 KiB/s 取整，但保底 1 KB/s，避免 91 B/s 被显示成 0 KB/s。
 */
object ApkDownloadProgress {
    fun text(
        downloaded: Long,
        total: Long,
        bytesPerSecond: Long,
        attempt: Int = 1,
    ): String {
        if (downloaded <= 0L) return "正在连接服务器…"

        val speed = formatSpeed(bytesPerSecond)
        val retry = if (attempt > 1) "（自动重试第 $attempt 次）" else ""
        return if (total > 0L) {
            val percent = ((downloaded.toDouble() / total.toDouble()) * 100.0)
                .coerceIn(0.0, 100.0).roundToInt()
            "${formatBytes(downloaded)} / ${formatBytes(total)} · $percent% · $speed$retry"
        } else {
            "${formatBytes(downloaded)} · $speed$retry"
        }
    }

    fun formatBytes(bytes: Long): String = when {
        bytes < 1024L -> "$bytes B"
        bytes < 1024L * 1024L -> "${bytes / 1024L} KB"
        else -> String.format("%.1f MB", bytes / (1024.0 * 1024.0))
    }

    /**
     * 瞬时速度。保底 1 KB/s：链路再慢也得让用户看出「还在动」，
     * 显示 0 KB/s 会被当成卡死。
     */
    private fun formatSpeed(bytesPerSecond: Long): String {
        if (bytesPerSecond < 1024L) {
            return if (bytesPerSecond <= 0L) "…" else "1 KB/s"
        }
        if (bytesPerSecond < 1024L * 1024L) return "${bytesPerSecond / 1024L} KB/s"
        return String.format("%.1f MB/s", bytesPerSecond / (1024.0 * 1024.0))
    }
}

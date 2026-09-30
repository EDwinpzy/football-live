package com.qiubo.optimaltv.apkupdate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 更新弹窗进度文案（2026-10-01）。
 *
 * 实测真机卡在「正在下载 0%」：弱网下 OkHttp 在 readTimeout 内一个字节都收不到，
 * 进度永远停在初始值，用户以为应用挂了。这里锁定两件事：
 * 1. 连接阶段要说人话（「正在连接服务器」），不能假装已经在下载 0%；
 * 2. 下载阶段要给出已下载字节与瞬时速度，让停滞可被察觉，而不是静默等待。
 */
class ApkDownloadProgressTest {

    @Test
    fun `尚未收到首字节时显示连接中而不是下载 0%`() {
        assertEquals(
            "正在连接服务器…",
            ApkDownloadProgress.text(0L, 165_845_605L, bytesPerSecond = 0L),
        )
    }

    @Test
    fun `下载中显示已下载量与总量`() {
        val text = ApkDownloadProgress.text(
            downloaded = 12L * 1024 * 1024,
            total = 165_845_605L,
            bytesPerSecond = 2L * 1024 * 1024,
        )
        assertTrue(text, text.contains("12.0 MB / 158.2 MB"))
        assertTrue(text, text.contains("8%"))
    }

    @Test
    fun `速度极慢时仍如实显示而不是四舍五入成 0 KB 每秒`() {
        val text = ApkDownloadProgress.text(
            downloaded = 93_945L,
            total = 165_845_605L,
            bytesPerSecond = 91L,
        )
        // 91 B/s 四舍五入会变成 0，必须保底显示至少 1 KB/s 让人知道还在动
        assertTrue(text, text.contains("KB/s"))
        assertTrue(text, text.contains("1 KB/s"))
    }

    @Test
    fun `自动重试次数在文案里可见`() {
        val text = ApkDownloadProgress.text(
            downloaded = 1024L * 1024,
            total = 165_845_605L,
            bytesPerSecond = 512L * 1024,
            attempt = 2,
        )
        assertTrue(text, text.contains("第 2 次"))
    }

    @Test
    fun `总量未知时只显示已下载量不显示百分比`() {
        val text = ApkDownloadProgress.text(
            downloaded = 5L * 1024 * 1024,
            total = 0L,
            bytesPerSecond = 1024L * 1024,
        )
        assertTrue(text, text.contains("5.0 MB"))
        assertTrue(!text.contains("%"))
    }

    @Test
    fun `字节数按 MiB 与 KiB 正确换算`() {
        assertEquals("1.0 MB", ApkDownloadProgress.formatBytes(1024L * 1024))
        assertEquals("512 KB", ApkDownloadProgress.formatBytes(512L * 1024))
        assertEquals("0 B", ApkDownloadProgress.formatBytes(0L))
    }
}

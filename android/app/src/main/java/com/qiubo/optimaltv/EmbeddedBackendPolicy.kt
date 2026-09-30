package com.qiubo.optimaltv

import java.io.File

/**
 * 内嵌后端解压目录的复用判定（2026-09-30 抽出以便单测）。
 *
 * 事故背景：热更包启动崩溃被隔离后，`.extracted` 标记仍写着热更签名
 * （如 `hot|30|40972839`），下次启动 [EmbeddedBackend.extractZipIfNeeded]
 * 会命中标记直接复用热更留下的解压目录，于是**内置 assets 后端永远不生效**。
 * 现场表现是 APK 已升级、但设备仍在跑旧后端逻辑。
 *
 * 修复：热更包被隔离时（marker 与 zip 一并作废），当前解压目录的标记就不再代表
 * 任何可用来源，必须连同标记一起删除，让回退路径重新解压 assets。
 */
internal object EmbeddedBackendPolicy {
    const val MARKER_NAME = ".extracted"

    /** 是否可以复用已解压目录。标记不存在时一律重新解压。 */
    fun canReuse(currentMarker: String?, wantedSignature: String): Boolean =
        !currentMarker.isNullOrEmpty() && currentMarker == wantedSignature

    /**
     * 坏热更包被隔离后清理解压目录标记，返回是否发生了清理。
     * 只删标记与目录内容，调用方随后走 assets 重新解压。
     */
    fun invalidateAfterQuarantine(backendDir: File): Boolean {
        val flag = File(backendDir, MARKER_NAME)
        if (!flag.exists()) return false
        backendDir.deleteRecursively()
        return !flag.exists()
    }
}

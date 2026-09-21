package com.qiubo.optimaltv.apkupdate

/** APK 分片：客户端按数组顺序下载并写入同一个文件，最后仍校验整包。 */
data class ApkPart(
    val url: String,
    val sizeBytes: Long,
    val sha256: String,
)

data class ApkArtifact(
    val fileName: String,
    val url: String,
    val sizeBytes: Long,
    val sha256: String,
    val certificateSha256: List<String> = emptyList(),
    /**
     * 分片清单（2026-09-21）：对象桶单对象硬上限 100 MiB（平台级），两端 APK 约 157 MiB，
     * 因此大包按 < 100 MiB 拆段下发。空列表表示单对象直连（沿用 `url`）。
     */
    val parts: List<ApkPart> = emptyList(),
)

data class ApkUpdateManifest(
    val packageName: String,
    val channel: String,
    val deviceType: String,
    val versionCode: Long,
    val versionName: String,
    val minSupportedVersionCode: Long,
    val forceUpdate: Boolean,
    val title: String,
    val changelog: List<String>,
    val apk: ApkArtifact,
)

enum class ApkUpdateDecision {
    NONE,
    OPTIONAL,
    REQUIRED,
}

object ApkPartPolicy {
    const val MAX_PARTS = 16

    /** 分片清单必须自洽：段数 1..MAX、逐段有长度、总长度等于整包声明大小。返回错误说明或 null。 */
    fun problem(parts: List<ApkPart>, totalBytes: Long): String? {
        if (parts.isEmpty()) return null
        if (parts.size > MAX_PARTS) return "APK 分片数量超过 $MAX_PARTS 段"
        if (parts.any { it.sizeBytes < 1 }) return "APK 分片大小无效"
        if (totalBytes < 1) return "APK 大小无效"
        if (parts.sumOf { it.sizeBytes } != totalBytes) return "APK 分片总大小与整包不一致"
        return null
    }
}

object ApkUpdatePolicy {
    fun decide(localVersionCode: Long, manifest: ApkUpdateManifest): ApkUpdateDecision {
        if (manifest.versionCode <= localVersionCode) return ApkUpdateDecision.NONE
        return if (manifest.forceUpdate || localVersionCode < manifest.minSupportedVersionCode) {
            ApkUpdateDecision.REQUIRED
        } else {
            ApkUpdateDecision.OPTIONAL
        }
    }
}

package com.qiubo.optimaltv.apkupdate

data class ApkArtifact(
    val fileName: String,
    val url: String,
    val sizeBytes: Long,
    val sha256: String,
    val certificateSha256: List<String> = emptyList(),
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

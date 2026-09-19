package com.qiubo.optimaltv.apkupdate

import org.junit.Assert.assertEquals
import org.junit.Test

class ApkUpdatePolicyTest {
    @Test
    fun newerManifestProducesOptionalUpdate() {
        val manifest = manifest(versionCode = 35, minSupportedVersionCode = 34)

        assertEquals(
            ApkUpdateDecision.OPTIONAL,
            ApkUpdatePolicy.decide(localVersionCode = 34, manifest = manifest),
        )
    }

    @Test
    fun versionBelowMinimumProducesRequiredUpdate() {
        val manifest = manifest(versionCode = 35, minSupportedVersionCode = 35)

        assertEquals(
            ApkUpdateDecision.REQUIRED,
            ApkUpdatePolicy.decide(localVersionCode = 34, manifest = manifest),
        )
    }

    @Test
    fun sameVersionDoesNotProduceUpdate() {
        val manifest = manifest(versionCode = 34, minSupportedVersionCode = 30)

        assertEquals(
            ApkUpdateDecision.NONE,
            ApkUpdatePolicy.decide(localVersionCode = 34, manifest = manifest),
        )
    }

    @Test
    fun forcedManifestProducesRequiredUpdateWhenNewer() {
        val manifest = manifest(versionCode = 35, minSupportedVersionCode = 30, forceUpdate = true)

        assertEquals(
            ApkUpdateDecision.REQUIRED,
            ApkUpdatePolicy.decide(localVersionCode = 34, manifest = manifest),
        )
    }

    private fun manifest(
        versionCode: Long,
        minSupportedVersionCode: Long,
        forceUpdate: Boolean = false,
    ) = ApkUpdateManifest(
        packageName = "com.qiubo.optimaltv.public",
        channel = "stable",
        deviceType = "tv",
        versionCode = versionCode,
        versionName = "1.$versionCode",
        minSupportedVersionCode = minSupportedVersionCode,
        forceUpdate = forceUpdate,
        title = "发现新版本",
        changelog = listOf("修复问题"),
        apk = ApkArtifact(
            fileName = "MyTV.apk",
            url = "https://download.example/MyTV.apk",
            sizeBytes = 1,
            sha256 = "a".repeat(64),
        ),
    )
}

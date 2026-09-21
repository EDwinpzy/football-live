package com.qiubo.optimaltv.apkupdate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
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

    @Test
    fun partSizesMustAddUpToWholePackage() {
        val parts = listOf(
            ApkPart(url = "https://download.example/p1", sizeBytes = 60, sha256 = "a".repeat(64)),
            ApkPart(url = "https://download.example/p2", sizeBytes = 40, sha256 = "b".repeat(64)),
        )

        assertNull(ApkPartPolicy.problem(parts, totalBytes = 100))
        assertEquals("APK 分片总大小与整包不一致", ApkPartPolicy.problem(parts, totalBytes = 101))
        // 空分片列表即单对象清单，策略层不介入
        assertNull(ApkPartPolicy.problem(emptyList(), totalBytes = 100))
    }

    @Test
    fun partCountAndSizesAreBounded() {
        val tooMany = List(ApkPartPolicy.MAX_PARTS + 1) {
            ApkPart(url = "https://download.example/p$it", sizeBytes = 1, sha256 = "c".repeat(64))
        }

        assertEquals(
            "APK 分片数量超过 ${ApkPartPolicy.MAX_PARTS} 段",
            ApkPartPolicy.problem(tooMany, totalBytes = tooMany.size.toLong()),
        )
        assertEquals(
            "APK 分片大小无效",
            ApkPartPolicy.problem(
                listOf(ApkPart(url = "https://download.example/p1", sizeBytes = 0, sha256 = "d".repeat(64))),
                totalBytes = 0,
            ),
        )
    }

    @Test
    fun shardedManifestDrivesUpdateFromParts() {
        val parts = listOf(
            ApkPart(url = "https://download.example/p1", sizeBytes = 60, sha256 = "e".repeat(64)),
            ApkPart(url = "https://download.example/p2", sizeBytes = 40, sha256 = "f".repeat(64)),
        )
        val manifest = manifest(
            versionCode = 36,
            minSupportedVersionCode = 30,
            url = "",
            sizeBytes = 100,
            parts = parts,
        )

        assertEquals(2, manifest.apk.parts.size)
        assertEquals(ApkUpdateDecision.OPTIONAL, ApkUpdatePolicy.decide(localVersionCode = 35, manifest = manifest))
    }

    @Test
    fun dismissedOptionalUpdateOnlyPromptedAgainManually() {
        assertFalse(
            ApkUpdatePolicy.shouldPrompt(
                ApkUpdateDecision.OPTIONAL, dismissed = true, manual = false,
            ),
        )
        assertTrue(
            ApkUpdatePolicy.shouldPrompt(
                ApkUpdateDecision.OPTIONAL, dismissed = true, manual = true,
            ),
        )
    }

    @Test
    fun requiredUpdateIgnoresDismissedVersion() {
        assertTrue(
            ApkUpdatePolicy.shouldPrompt(
                ApkUpdateDecision.REQUIRED, dismissed = true, manual = false,
            ),
        )
    }

    @Test
    fun sameVersionNeverPromptsEvenManually() {
        assertFalse(
            ApkUpdatePolicy.shouldPrompt(
                ApkUpdateDecision.NONE, dismissed = false, manual = true,
            ),
        )
    }

    private fun manifest(
        versionCode: Long,
        minSupportedVersionCode: Long,
        forceUpdate: Boolean = false,
        url: String = "https://download.example/MyTV.apk",
        sizeBytes: Long = 1,
        parts: List<ApkPart> = emptyList(),
    ) = ApkUpdateManifest(
        packageName = "com.qiubo.optimaltv.mobile.public",
        channel = "stable",
        deviceType = "mobile",
        versionCode = versionCode,
        versionName = "1.$versionCode",
        minSupportedVersionCode = minSupportedVersionCode,
        forceUpdate = forceUpdate,
        title = "发现新版本",
        changelog = listOf("修复问题"),
        apk = ApkArtifact(
            fileName = "MyTV.apk",
            url = url,
            sizeBytes = sizeBytes,
            sha256 = "a".repeat(64),
            parts = parts,
        ),
    )
}

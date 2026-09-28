package com.qiubo.optimaltv.data.repo

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CatalogOriginPolicyTest {
    @Test
    fun `目录快照只接受豆瓣卡片`() {
        assertTrue(CatalogOriginPolicy.allows("douban", "douban:1292052"))
        assertFalse(CatalogOriginPolicy.allows("mcms", "mcms:12345"))
        assertFalse(CatalogOriginPolicy.allows("hhkan", "hhkan:12345"))
        assertFalse(CatalogOriginPolicy.allows("", "douban:1292052"))
    }

    @Test
    fun `持久化快照严格 v4 而内置 seed 接受 v3 和 v4`() {
        assertEquals(setOf(3, 4), CatalogOriginPolicy.SEED_ACCEPTED_VERSIONS)
        assertEquals(setOf(CatalogOriginPolicy.SNAPSHOT_VERSION), CatalogOriginPolicy.PERSISTED_ACCEPTED_VERSIONS)
        assertTrue(3 in CatalogOriginPolicy.SEED_ACCEPTED_VERSIONS)
        assertFalse(3 in CatalogOriginPolicy.PERSISTED_ACCEPTED_VERSIONS)
        assertTrue(4 in CatalogOriginPolicy.PERSISTED_ACCEPTED_VERSIONS)
    }
}

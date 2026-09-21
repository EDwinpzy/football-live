package com.qiubo.optimaltv.data.source

import org.junit.Assert.assertEquals
import org.junit.Test

class VodRefreshRoutesTest {
    @Test
    fun homeRefreshAddsCacheBypassFlag() {
        assertEquals(
            "/vod/home?category=movie&refresh=1",
            VodApiSource.Companion.VodRoutes.home("movie", refresh = true),
        )
    }

    @Test
    fun detailRefreshKeepsDeferFlagAndAddsCacheBypassFlag() {
        assertEquals(
            "/vod/detail/1292052?defer_sources=1&refresh=1",
            VodApiSource.Companion.VodRoutes.detail("douban:1292052", deferSources = true, refresh = true),
        )
    }
}

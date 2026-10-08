package com.qiubo.optimaltv.data.source

import org.junit.Assert.assertEquals
import org.junit.Test

class VodRefreshRoutesTest {
    @Test
    fun posterRelayBypassesPreviouslyCachedBlankImages() {
        assertEquals(
            "http://127.0.0.1:8090/hhkan/proxy?u=https%3A%2F%2Fimg3.doubanio.com%2Fp.jpg&poster_cache=2",
            VodApiSource.relayPoster("http://127.0.0.1:8090", "https://img3.doubanio.com/p.jpg"),
        )
        assertEquals("https://other.test/p.jpg", VodApiSource.relayPoster("http://127.0.0.1:8090", "https://other.test/p.jpg"))
    }

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

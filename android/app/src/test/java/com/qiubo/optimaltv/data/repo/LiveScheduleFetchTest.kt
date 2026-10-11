package com.qiubo.optimaltv.data.repo

import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class LiveScheduleFetchTest {
    @Test
    fun matchesRequestsUseDirectPathFirstAndFlagOnlyTheProxyRetry() {
        assertEquals("/api/matches?fresh=1", LiveRepository.matchesRequestPath(footballVpn = false))
        assertEquals("/api/matches?fresh=1&football_vpn=1", LiveRepository.matchesRequestPath(footballVpn = true))
    }

    @Test
    fun errorPayloadWithEmptyMatchesTriggersFootballProxyRetry() = runBlocking {
        var proxyStarted = false
        val result = fetchMatchesWithFootballProxyFallback(
            directRequest = {
                LiveRepository.matchesArray(JSONObject("""{"error":"feed unavailable","matches":[]}"""))
            },
            startFootballProxy = { proxyStarted = true },
            proxyRequest = { JSONArray("""[{"match_id":"123"}]""") },
        )

        assertTrue(proxyStarted)
        assertTrue(result.usedFootballProxy)
        assertEquals(1, result.value.length())
    }

    @Test
    fun successfulDirectEmptyListDoesNotStartProxyAndIsAccepted() = runBlocking {
        var proxyStarted = false
        val result = fetchMatchesWithFootballProxyFallback(
            directRequest = { LiveRepository.matchesArray(JSONObject("""{"matches":[]}""")) },
            startFootballProxy = { proxyStarted = true },
            proxyRequest = { JSONArray() },
        )

        assertFalse(proxyStarted)
        assertFalse(result.usedFootballProxy)
        assertEquals(0, result.value.length())
    }

    @Test
    fun rememberedProxyPreferenceUsesProxyFirstAndFallsBackToDirect() = runBlocking {
        val attempts = mutableListOf<String>()
        val result = fetchMatchesWithFootballProxyFallback(
            directRequest = { attempts += "direct"; JSONArray("[]") },
            startFootballProxy = { attempts += "start-proxy" },
            proxyRequest = { attempts += "proxy"; throw IllegalStateException("proxy offline") },
            preferFootballProxy = true,
        )

        assertEquals(listOf("start-proxy", "proxy", "direct"), attempts)
        assertFalse(result.usedFootballProxy)
    }

    @Test
    fun bothDirectAndProxyFailuresAreReported() = runBlocking {
        val error = runCatching {
            fetchMatchesWithFootballProxyFallback(
                directRequest = { throw IllegalStateException("direct timeout") },
                startFootballProxy = { throw IllegalStateException("足球代理无法启动") },
                proxyRequest = { JSONArray() },
            )
        }.exceptionOrNull()

        assertTrue(error is IllegalStateException)
        assertTrue(error?.message.orEmpty().contains("direct timeout"))
        assertTrue(error?.message.orEmpty().contains("足球代理重试失败"))
        assertTrue(error?.message.orEmpty().contains("足球代理无法启动"))
    }
}

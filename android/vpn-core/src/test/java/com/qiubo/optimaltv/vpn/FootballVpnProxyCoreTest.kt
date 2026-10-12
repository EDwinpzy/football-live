package com.qiubo.optimaltv.vpn

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class FootballVpnProxyCoreTest {
    private val defaultYaml = """
        allow-lan: false
        proxy-providers:
          subscription_1:
            type: http
            url: "https://old.example/one?token=old1"
            path: ./providers/subscription_1.yaml
            health-check:
              url: https://www.gstatic.com/generate_204
          subscription_2:
            type: http
            url: "https://old.example/two?token=old2"
            path: ./providers/subscription_2.yaml
            health-check:
              url: https://www.gstatic.com/generate_204
          subscription_3:
            type: http
            url: "https://old.example/three?token=old3"
            path: ./providers/subscription_3.yaml
            health-check:
              url: https://www.gstatic.com/generate_204
        proxy-groups:
          - name: FOOTBALL-NODES
            type: fallback
        rules:
          - MATCH,FOOTBALL-NODES
        listeners:
          - name: android-football-only
            listen: 127.0.0.1
            port: 17890
    """.trimIndent()

    @Test
    fun replaceSubscriptionUrlsChangesOnlyTheThreeProviderUrls() {
        val subscriptions = listOf(
            "https://new.example/one?token=one",
            "https://new.example/two?token=two",
            "https://new.example/three?token=three",
        )

        val updated = FootballVpnProxyCore.replaceSubscriptionUrls(defaultYaml, subscriptions)

        assertTrue(updated!!.contains("url: \"${subscriptions[0]}\""))
        assertTrue(updated.contains("url: \"${subscriptions[1]}\""))
        assertTrue(updated.contains("url: \"${subscriptions[2]}\""))
        assertEquals(3, Regex("url: \\\"https://new\\.example/").findAll(updated).count())
        assertEquals(3, Regex("url: https://www\\.gstatic\\.com/generate_204").findAll(updated).count())
        assertTrue(updated.contains("- MATCH,FOOTBALL-NODES"))
        assertTrue(updated.contains("- name: android-football-only"))
        assertTrue(updated.contains("port: 17890"))
    }

    @Test
    fun rejectsIncompleteOrUnsafeSubscriptionLists() {
        assertNull(FootballVpnProxyCore.replaceSubscriptionUrls(defaultYaml, listOf("https://only.example")))
        assertNull(
            FootballVpnProxyCore.replaceSubscriptionUrls(
                defaultYaml,
                listOf("https://one.example", "https://two.example", "https://bad.example/\"\n  rules: []"),
            ),
        )
        assertFalse(FootballVpnProxyCore.isValidSubscriptionUrl("http://plain.example/subscription"))
        assertFalse(FootballVpnProxyCore.isValidSubscriptionUrl("https://example.com/line\n    rules: []"))
        assertTrue(FootballVpnProxyCore.isValidSubscriptionUrl("https://example.com/api/subscribe?token=a%2Fb"))
    }

    @Test
    fun versionZeroDefaultsMayOmitUpdatedAtButSavedVersionsNeedIt() {
        val subscriptions = """
            "subscriptions": [
              "https://one.example/sub",
              "https://two.example/sub",
              "https://three.example/sub"
            ]
        """.trimIndent()

        val defaults = FootballVpnProxyCore.parseRemoteConfig(
            """{"ret":0,"version":0,"updatedAt":null,$subscriptions}""",
        )
        assertNotNull(defaults)
        assertEquals("0", defaults!!.version)
        assertEquals("", defaults.updatedAt)

        assertNull(
            FootballVpnProxyCore.parseRemoteConfig(
                """{"ret":0,"version":1,"updatedAt":null,$subscriptions}""",
            ),
        )
        val saved = FootballVpnProxyCore.parseRemoteConfig(
            """{"ret":0,"version":1,"updatedAt":"2026-10-11T01:02:03Z",$subscriptions}""",
        )
        assertNotNull(saved)
        assertEquals("2026-10-11T01:02:03Z", saved!!.updatedAt)
    }

    @Test
    fun cachedRuntimeConfigAddsLoopbackControllerAndMigratesFallbackToSelectableGroup() {
        val secret = "a".repeat(64)

        val migrated = FootballVpnProxyCore.prepareRuntimeConfig(defaultYaml, secret)

        assertNotNull(migrated)
        assertTrue(migrated!!.contains("external-controller: 127.0.0.1:17891"))
        assertTrue(migrated.contains("secret: \"$secret\""))
        assertTrue(migrated.contains("name: FOOTBALL-NODES\n    type: select"))
        assertFalse(migrated.contains("name: FOOTBALL-NODES\n            type: fallback"))
        assertFalse(migrated.contains("            url: https://www.gstatic.com/generate_204"))
        assertFalse(migrated.contains("            interval: 300"))
        assertFalse(migrated.contains("            timeout: 5000"))
        assertTrue(migrated.contains("- MATCH,FOOTBALL-NODES"))
        assertTrue(migrated.contains("port: 17890"))
    }

    @Test
    fun proxyCandidateParserKeepsOnlyConcreteMembersOfFootballGroup() {
        val response = """
            {"proxies": {
              "FOOTBALL-NODES": {"type":"Selector","all":["节点 A","DIRECT","nested","REJECT","missing"]},
              "节点 A": {"type":"Shadowsocks"},
              "DIRECT": {"type":"Direct"},
              "nested": {"type":"URLTest"},
              "REJECT": {"type":"Reject"}
            }}
        """.trimIndent()

        assertEquals(listOf("节点 A"), FootballVpnProxyCore.parseProxyCandidates(response))
        assertNull(FootballVpnProxyCore.parseProxyCandidates("{}"))
    }

    @Test
    fun delayParserAndSelectorRejectUnavailableAndChooseSmallestPositiveLatency() {
        assertEquals(83, FootballVpnProxyCore.parseDelayResponse("""{"delay":83}"""))
        assertNull(FootballVpnProxyCore.parseDelayResponse("""{"delay":0}"""))
        assertNull(FootballVpnProxyCore.parseDelayResponse("""{"error":"timeout"}"""))

        val fastest = FootballVpnProxyCore.selectLowestLatency(
            listOf(
                FootballVpnProxyCore.NodeLatency("slow", 420),
                FootballVpnProxyCore.NodeLatency("failed", 0),
                FootballVpnProxyCore.NodeLatency("fast", 83),
            ),
        )
        assertEquals("fast", fastest?.name)
        assertEquals(83, fastest?.delayMs)
        assertNull(FootballVpnProxyCore.selectLowestLatency(listOf(FootballVpnProxyCore.NodeLatency("failed", 0))))
    }

    @Test
    fun providerOnlyNodesAreMeasuredUsingProviderEndpoint() {
        val proxies = """{"proxies":{"FOOTBALL-NODES":{"all":["node/a","DIRECT","group","missing"]}}}"""
        val providers = """{"providers":{"sub one":{"proxies":[{"name":"node/a","type":"Shadowsocks"},{"name":"DIRECT","type":"Direct"},{"name":"group","type":"Selector"}]}}}"""
        assertEquals(listOf("node/a"), FootballVpnProxyCore.parseProxyCandidates(proxies, providers))
        assertEquals("/providers/proxies/sub%20one/node%2Fa/healthcheck", FootballVpnProxyCore.nodeDelayPath("node/a", "sub one"))
        assertEquals("/proxies/node%2Fa/delay", FootballVpnProxyCore.nodeDelayPath("node/a", null))
    }

    @Test
    fun matchProbeUrlRequiresHttpsHostAndDropsCredentialsAndFragment() {
        assertEquals(
            "https://www.88zhibo.tv/live?id=42",
            FootballVpnProxyCore.normalizeProbeUrl("https://www.88zhibo.tv/live?id=42#player"),
        )
        assertNull(FootballVpnProxyCore.normalizeProbeUrl("http://www.88zhibo.tv/"))
        assertNull(FootballVpnProxyCore.normalizeProbeUrl("https://user:pass@www.88zhibo.tv/"))
    }

    @Test
    fun nodeSelectionStopsForCoroutineCancellationTaskInterruptionOrSupersededMatch() {
        assertFalse(FootballVpnProxyCore.shouldCancelSelection(false, false, false))
        assertTrue(FootballVpnProxyCore.shouldCancelSelection(true, false, false))
        assertTrue(FootballVpnProxyCore.shouldCancelSelection(false, true, false))
        assertTrue(FootballVpnProxyCore.shouldCancelSelection(false, false, true))
    }

    @Test
    fun goRuntimeReservesOneCpuAndCapsParallelismAtTwo() {
        assertEquals(1, FootballVpnProxyCore.goMaxProcsForAvailableProcessors(1))
        assertEquals(1, FootballVpnProxyCore.goMaxProcsForAvailableProcessors(2))
        assertEquals(2, FootballVpnProxyCore.goMaxProcsForAvailableProcessors(3))
        assertEquals(2, FootballVpnProxyCore.goMaxProcsForAvailableProcessors(16))
    }

    @Test
    fun coreCommandUsesNiceWhenAvailableAndPreservesDirectExecFallback() {
        val arguments = listOf("-d", "/data/user/0/app/files", "-f", "football.yaml")

        assertEquals(
            listOf("/system/bin/nice", "-n", "10", "/libmihomo.so") + arguments,
            FootballVpnProxyCore.coreProcessCommand(
                "/libmihomo.so",
                arguments,
                niceExecutable = "/system/bin/nice",
            ),
        )
        assertEquals(
            listOf("/libmihomo.so") + arguments,
            FootballVpnProxyCore.coreProcessCommand("/libmihomo.so", arguments, niceExecutable = null),
        )
    }
}

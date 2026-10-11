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
}

package com.qiubo.optimaltv.data.source

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.time.Duration

/**
 * 云端 Resolver 契约解析。
 *
 * 重点是**回落语义**：云端拿不到结果时必须优雅退到端侧 `/vod/detail`
 * （端侧实测 ~0.9s 能匹配出 12 条线），绝不能把功能改坏成空片源。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class SourceResolveTest {

    private fun parse(raw: String, mytvId: Long = 1L, episode: Int = 1) =
        VodApiSource.parseResolve(JSONObject(raw), mytvId, episode)

    @Test
    fun parsesAFullMatchWithPreferredAndBackups() {
        val result = parse(
            "{\"mytv_id\":10000001,\"episode\":1,\"resolve_version\":1790216157," +
                "\"preferred\":{\"source_id\":\"hhkan\",\"source_name\":\"好好看\"," +
                "\"content_id\":\"vid-1\",\"episode_index\":0,\"match_score\":0.93,\"quality\":{}}," +
                "\"backups\":[{\"source_id\":\"other\",\"content_id\":\"vid-2\",\"episode_index\":0}]," +
                "\"cache\":{\"kind\":\"static_index\",\"age_ms\":123},\"status\":\"MATCHED\"}",
            mytvId = 10000001L,
        )
        assertTrue(result.usable)
        assertEquals("hhkan", result.preferred?.sourceId)
        assertEquals("vid-1", result.preferred?.contentId)
        // linePrefix 与 detail 线路 id "{sourceId}:{contentId}:{epIndex}" 前两段同构
        assertEquals("hhkan:vid-1", result.preferred?.linePrefix)
        assertEquals(1, result.backups.size)
        assertEquals("other:vid-2", result.backups[0].linePrefix)
        assertEquals("static_index", result.cacheKind)
        assertEquals(123L, result.cacheAgeMs)
    }

    @Test
    fun staleAvailableIsStillUsable() {
        // 静态索引过期但仍可播——按可用处理，否则白白回落一轮慢路径。
        val result = parse(
            "{\"mytv_id\":1,\"episode\":1," +
                "\"preferred\":{\"source_id\":\"s\",\"content_id\":\"c\"},\"status\":\"STALE_AVAILABLE\"}",
        )
        assertTrue(result.usable)
    }

    @Test
    fun nonMatchedStatusesAreNotUsableAndMustFallBack() {
        for (status in listOf(
            "RESOLVING", "FAST_PATH_FAILED", "FALLBACK_SEARCHING", "TEMPORARILY_UNAVAILABLE",
        )) {
            val result = parse(
                "{\"mytv_id\":1,\"episode\":1," +
                    "\"preferred\":{\"source_id\":\"s\",\"content_id\":\"c\"},\"status\":\"$status\"}",
            )
            // 关键：非 MATCHED / STALE 一律不可用 → 上层回落 detail
            assertFalse("$status 不应被判为可用", result.usable)
        }
    }

    @Test
    fun temporarilyUnavailableWithNoCandidatesIsTheObservedRealResponse() {
        // 线上实测到的就是这一份 payload
        val result = parse(
            "{\"mytv_id\":10000001,\"episode\":1,\"resolve_version\":1790216157," +
                "\"preferred\":null,\"backups\":[],\"cache\":{\"kind\":\"static_index\",\"age_ms\":123}," +
                "\"status\":\"TEMPORARILY_UNAVAILABLE\"}",
            mytvId = 10000001L,
        )
        assertFalse(result.usable)
        assertFalse(result.hasCandidates)
        assertNull(result.preferred)
        assertTrue(result.diagnostic.contains("TEMPORARILY_UNAVAILABLE"))
        assertTrue(result.diagnostic.contains("static_index"))
    }

    @Test
    fun matchedWithoutPreferredIsNotUsable() {
        // status 说 MATCHED 但没有 preferred：不能当作可用，否则会拿到空线路。
        val result = parse("{\"mytv_id\":1,\"episode\":1,\"preferred\":null,\"backups\":[],\"status\":\"MATCHED\"}")
        assertFalse(result.usable)
    }

    @Test
    fun blankStatusDegradesToTemporarilyUnavailable() {
        val result = parse("{\"mytv_id\":1,\"episode\":1}")
        assertEquals("TEMPORARILY_UNAVAILABLE", result.status)
        assertFalse(result.usable)
    }

    @Test
    fun candidatesMissingEitherIdAreDropped() {
        // 半截候选不能进列表：缺一就无法构造 playRef 前缀。
        val result = parse(
            "{\"mytv_id\":1,\"episode\":1,\"status\":\"MATCHED\"," +
                "\"preferred\":{\"source_id\":\"\",\"content_id\":\"c\"}," +
                "\"backups\":[{\"source_id\":\"s\",\"content_id\":\"\"}," +
                "{\"source_id\":\"ok\",\"content_id\":\"good\"}]}",
        )
        assertNull(result.preferred)
        assertEquals(1, result.backups.size)
        assertEquals("good", result.backups[0].contentId)
    }

    @Test
    fun missingEpisodeIndexFallsBackToTheRequestedEpisode() {
        // 后端 episode 是 1-based；候选漏传 episode_index 时不能当成第 0 集。
        val result = parse(
            "{\"mytv_id\":1,\"episode\":7,\"status\":\"MATCHED\"," +
                "\"preferred\":{\"source_id\":\"s\",\"content_id\":\"c\"}}",
            episode = 7,
        )
        assertEquals(6, result.preferred?.episodeIndex)
    }

    @Test
    fun routeUsesOneBasedEpisodeAndMytvId() {
        val routes = VodApiSource.Companion.VodRoutes
        assertEquals("/vod/resolve/10000001?episode=1", routes.resolve(10000001L, 1))
        assertEquals("/vod/resolve/10000001?episode=5", routes.resolve(10000001L, 5))
        // 越界的 0/负数被夹到 1，不给后端发非法参数
        assertEquals("/vod/resolve/10000001?episode=1", routes.resolve(10000001L, 0))
        assertEquals("/vod/resolve/10000001?episode=1", routes.resolve(10000001L, -3))
    }

    @Test
    fun nonPositiveMytvIdReturnsNullInsteadOfHittingTheNetwork() {
        val client = okhttp3.OkHttpClient()
        assertNull(VodApiSource.fetchResolve("http://127.0.0.1:1", client, 0L, 1))
        assertNull(VodApiSource.fetchResolve("http://127.0.0.1:1", client, -5L, 1))
    }

    @Test
    fun unreachableBaseReturnsNullSoCallerFallsBack() {
        // 端点挂了也必须返回 null（而不是抛异常），上层据此回落 detail。
        val client = okhttp3.OkHttpClient.Builder().callTimeout(Duration.ofMillis(800)).build()
        assertNull(VodApiSource.fetchResolve("http://127.0.0.1:9", client, 10000001L, 1))
    }
}

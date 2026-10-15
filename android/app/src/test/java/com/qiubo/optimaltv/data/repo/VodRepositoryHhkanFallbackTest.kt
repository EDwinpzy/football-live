package com.qiubo.optimaltv.data.repo

import android.content.Context
import java.io.File
import com.qiubo.optimaltv.data.prefs.SettingsStore
import kotlinx.coroutines.runBlocking
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RuntimeEnvironment
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.concurrent.atomic.AtomicInteger

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class VodRepositoryHhkanFallbackTest {
    @Test
    fun `cache reread exposes an episode added on another source without refreshing upstream`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val directory = File(context.filesDir, "metadata-catalog")
        directory.deleteRecursively()
        val snapshot = File(context.filesDir, "catalog_snapshot.json")
        snapshot.delete()
        val settings = SettingsStore(context)
        settings.setHhkanBase("http://hhkan.test")
        val requestedUrls = mutableListOf<String>()
        val requestCount = AtomicInteger()
        fun source(name: String, episodeCount: Int, pid: Int): String {
            val episodes = (1..episodeCount).joinToString(",") { index ->
                "{\"ep\":\"第${index}集\",\"pid\":$pid,\"vid\":${pid * 100 + index}}"
            }
            return "{\"name\":\"$name\",\"episodes\":[$episodes]}"
        }
        fun detail(vararg sources: String) =
            "{\"title\":\"测试剧\",\"year\":\"2025\",\"sources\":[${sources.joinToString(",")}] }"
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            val url = chain.request().url
            requestedUrls += url.encodedPath + url.encodedQuery?.let { "?$it" }.orEmpty()
            val body = if (requestCount.getAndIncrement() == 0) {
                detail(source("线路A", 15, 23))
            } else {
                detail(source("线路A", 15, 23), source("线路B", 16, 24))
            }
            Response.Builder()
                .request(chain.request())
                .protocol(Protocol.HTTP_1_1)
                .code(200)
                .message("OK")
                .body(body.toResponseBody("application/json".toMediaType()))
                .build()
        }.build()
        try {
            val repository = VodRepository(settings, client, context)
            val initial = repository.resolveDetail("hhkan:721")!!
            assertEquals(15, initial.episodes.size)

            val updated = repository.resolveDetail(
                "hhkan:721", forceRefresh = true, refreshSources = false,
            )!!

            assertEquals(listOf("/hhkan/detail/721", "/hhkan/detail/721"), requestedUrls)
            val selectionEpisodes = repository.episodesForSelection(updated, preferredLine = null)
            assertEquals(16, selectionEpisodes?.size)
            assertEquals("第16集", selectionEpisodes?.last()?.name)
        } finally {
            directory.deleteRecursively()
            snapshot.delete()
        }
    }

    @Test
    fun `directory-outside HHKan id resolves detail through native route`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val directory = File(context.filesDir, "metadata-catalog")
        directory.deleteRecursively()
        val snapshot = File(context.filesDir, "catalog_snapshot.json")
        snapshot.delete()
        val settings = SettingsStore(context)
        settings.setHhkanBase("http://hhkan.test")
        val requestedPaths = mutableListOf<String>()
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            requestedPaths += chain.request().url.encodedPath
            Response.Builder()
                .request(chain.request())
                .protocol(Protocol.HTTP_1_1)
                .code(200)
                .message("OK")
                .body(
                    """{"title":"目录外测试剧","year":"2025","desc":"详情简介","sources":[{"name":"线路A","episodes":[{"ep":"第1集","pid":23,"vid":45}]}]}"""
                        .toResponseBody("application/json".toMediaType()),
                )
                .build()
        }.build()
        try {
            val repository = VodRepository(settings, client, context)

            val detail = repository.resolveDetail("hhkan:721")

            assertEquals(listOf("/hhkan/detail/721"), requestedPaths)
            assertEquals("目录外测试剧", detail?.title)
            assertEquals(listOf("第1集"), detail?.episodes?.map { it.name })
        } finally {
            directory.deleteRecursively()
            snapshot.delete()
        }
    }

    @Test
    fun `directory-outside HHKan id can load a brief directly`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val directory = File(context.filesDir, "metadata-catalog")
        directory.deleteRecursively()
        val snapshot = File(context.filesDir, "catalog_snapshot.json")
        snapshot.delete()
        val settings = SettingsStore(context)
        settings.setHhkanBase("http://hhkan.test")
        val requestedPaths = mutableListOf<String>()
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            requestedPaths += chain.request().url.encodedPath
            Response.Builder()
                .request(chain.request())
                .protocol(Protocol.HTTP_1_1)
                .code(200)
                .message("OK")
                .body(
                    """{"title":"目录外测试剧","year":"2025","desc":"直接简介","meta":"2025 / 中国大陆","score":8.1,"sources":[]}"""
                        .toResponseBody("application/json".toMediaType()),
                )
                .build()
        }.build()
        try {
            val repository = VodRepository(settings, client, context)

            val brief = repository.vodBrief("hhkan:722")

            assertEquals(listOf("/hhkan/detail/722"), requestedPaths)
            assertEquals(Triple("2025 / 中国大陆", "直接简介", 8.1), brief)
        } finally {
            directory.deleteRecursively()
            snapshot.delete()
        }
    }
}

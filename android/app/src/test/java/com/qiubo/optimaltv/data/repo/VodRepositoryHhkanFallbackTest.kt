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

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class VodRepositoryHhkanFallbackTest {
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

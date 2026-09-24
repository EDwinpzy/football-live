package com.qiubo.optimaltv.data.repo

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import com.qiubo.optimaltv.BuildConfig
import com.qiubo.optimaltv.data.catalog.MetadataCatalogStore
import com.qiubo.optimaltv.data.prefs.SettingsStore
import java.io.File
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RuntimeEnvironment
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class MetadataCatalogRepositoryTest {
    @Test
    fun `离线片库筛选与分页读取本地目录`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val settings = SettingsStore(context)
        settings.setHhkanBase("")
        val repository = VodRepository(settings, OkHttpClient(), context)

        val filters = repository.allFilters(cid = 1)
        val page = repository.showPage(cid = 1, area = "中国大陆", by = "new", page = 1)

        assertTrue(filters.areas.contains("中国大陆"))
        assertTrue(filters.years.isNotEmpty())
        assertTrue(page?.items?.isNotEmpty() == true)
        assertTrue(page!!.items.all { it.area.contains("中国大陆") })
    }

    @Test
    fun `离线搜索与详情从本地目录恢复`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val settings = SettingsStore(context)
        settings.setHhkanBase("")
        val repository = VodRepository(settings, OkHttpClient(), context)
        repository.awaitCatalogAvailable()

        val match = repository.search("年会不能停").firstOrNull()
        assertTrue(match != null)
        val detail = repository.resolveDetail(match!!.id)

        assertEquals(match.title, detail?.title)
        assertEquals(match.mytvId, detail?.mytvId)
    }

    @Test
    fun `搜索通过本地 SQLite 拼音前缀检索并保持热度顺序`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val settings = SettingsStore(context)
        settings.setHhkanBase("")
        val store = MetadataCatalogStore(context, OkHttpClient(), BuildConfig.API_BASE_URL)
        val database = store.currentDatabaseFile
        database.parentFile?.deleteRecursively()
        database.parentFile?.mkdirs()
        writeSearchCatalog(database)

        try {
            val repository = VodRepository(settings, OkHttpClient(), context)
            val matches = repository.search("bwb")

            assertEquals(listOf("10000014", "10000013"), matches.map { it.mytvId })
            assertEquals(listOf("douban:1291547", "douban:1291546"), matches.map { it.id })
            assertEquals(listOf("10000013"), repository.search("1993").map { it.mytvId })
            assertEquals(listOf("10000014", "10000013"), repository.search("中国大陆").map { it.mytvId })
        } finally {
            database.parentFile?.deleteRecursively()
        }
    }

    @Test
    fun `离线时本地影视目录仍填充分类三栏`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val settings = SettingsStore(context)
        settings.setHhkanBase("")
        val repository = VodRepository(settings, OkHttpClient(), context)

        repository.awaitHome()
        val localHomeIds = repository.homeFlow.value.sections
            .firstOrNull { it.name == "最近热门" }
            ?.items
            ?.map { it.id }
            .orEmpty()
        assertTrue("内置目录应先提供离线首页数据", localHomeIds.isNotEmpty())

        val hasLibraryContent = repository.awaitLibBlocksContent("douban:movie", budgetMs = 1_000L)
        val libraryBlocks = repository.libBlocksFlow.value[1].orEmpty()

        assertTrue("已有本地目录时，分类三栏应能在无 API 网络时就绪", hasLibraryContent)
        assertEquals(listOf("最近热门", "最新上线", "最近更新"), libraryBlocks.map { it.name })
        assertTrue(libraryBlocks.first().items.isNotEmpty())
        assertTrue(libraryBlocks.first().items.all { it.id in localHomeIds })
        assertTrue(libraryBlocks.last().items.isEmpty())
    }

    private fun writeSearchCatalog(file: File) {
        val db = SQLiteDatabase.openOrCreateDatabase(file, null)
        db.execSQL("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("INSERT INTO meta VALUES ('schema_version', '3')")
        db.execSQL("INSERT INTO meta VALUES ('data_version', '2026092401')")
        db.execSQL("CREATE TABLE media (id INTEGER PRIMARY KEY, media_type TEXT, title TEXT, original_title TEXT, year INTEGER, overview TEXT, popularity REAL, updated_at INTEGER)")
        db.execSQL("INSERT INTO media VALUES (10000013, 'movie', '片甲', '', 1993, '', 5.0, 0)")
        db.execSQL("INSERT INTO media VALUES (10000014, 'movie', '片乙', '', 1994, '', 9.0, 0)")
        db.execSQL("CREATE TABLE external_id (media_id INTEGER, provider TEXT, external_id TEXT)")
        db.execSQL("INSERT INTO external_id VALUES (10000013, 'douban', '1291546')")
        db.execSQL("INSERT INTO external_id VALUES (10000014, 'douban', '1291547')")
        db.execSQL("CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT)")
        db.execSQL("CREATE TABLE credit (media_id INTEGER, person_id INTEGER, department TEXT, credit_order INTEGER)")
        db.execSQL("CREATE TABLE genre (id INTEGER PRIMARY KEY, name TEXT)")
        db.execSQL("CREATE TABLE media_genre (media_id INTEGER, genre_id INTEGER)")
        db.execSQL("CREATE TABLE region (id INTEGER PRIMARY KEY, name TEXT)")
        db.execSQL("INSERT INTO region VALUES (1, '中国大陆')")
        db.execSQL("CREATE TABLE media_region (media_id INTEGER, region_id INTEGER)")
        db.execSQL("INSERT INTO media_region VALUES (10000013, 1)")
        db.execSQL("INSERT INTO media_region VALUES (10000014, 1)")
        db.execSQL("CREATE TABLE rating (media_id INTEGER, provider TEXT, score REAL)")
        db.execSQL("CREATE TABLE image (media_id INTEGER, image_type TEXT, original_url TEXT)")
        db.execSQL("CREATE TABLE search_doc (media_id INTEGER, title_norm TEXT, aliases_norm TEXT, pinyin TEXT, pinyin_alts TEXT, initials TEXT, initials_alts TEXT, people_norm TEXT, people_pinyin TEXT, genres_norm TEXT, popularity REAL)")
        db.execSQL("INSERT INTO search_doc VALUES (10000013, '片甲', '', '', '', 'bwbj', '', '', '', '', 5.0)")
        db.execSQL("INSERT INTO search_doc VALUES (10000014, '片乙', '', '', '', 'bwbj', '', '', '', '', 9.0)")
        db.close()
    }
}

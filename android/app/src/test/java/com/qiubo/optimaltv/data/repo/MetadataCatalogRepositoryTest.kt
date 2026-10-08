package com.qiubo.optimaltv.data.repo

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import com.qiubo.optimaltv.BuildConfig
import com.qiubo.optimaltv.data.catalog.MetadataCatalogStore
import com.qiubo.optimaltv.data.prefs.SettingsStore
import java.io.File
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlin.concurrent.thread
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RuntimeEnvironment
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class MetadataCatalogRepositoryTest {
    @Test
    fun `有效空 SQLite 不恢复 APK seed 快照`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val directory = File(context.filesDir, "metadata-catalog")
        directory.deleteRecursively()
        directory.mkdirs()
        val database = File(directory, "metadata.sqlite")
        writeValidEmptyMetadataDatabase(database)
        val settings = SettingsStore(context)
        settings.setHhkanBase("")
        val offlineClient = OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder()
                .request(chain.request())
                .protocol(Protocol.HTTP_1_1)
                .code(503)
                .message("offline test")
                .body("{}".toResponseBody("application/json".toMediaType()))
                .build()
        }.build()

        try {
            val repository = VodRepository(settings, offlineClient, context)
            repository.refresh()

            assertTrue(repository.state.value.catalog?.dedupedItems?.isEmpty() == true)
            assertTrue(repository.search("年会不能停").isEmpty())
        } finally {
            directory.deleteRecursively()
        }
    }

    @Test
    fun `目录快照读取和写入都限定大小`() {
        assertEquals(120, CatalogSnapshotPolicy.boundedItems(List(500) { index ->
            com.qiubo.optimaltv.data.model.VodItem("douban:$index", "douban", "条目$index", "douban:movie")
        }).size)
        assertTrue(CatalogSnapshotPolicy.isReadableSize(1L))
        assertTrue(!CatalogSnapshotPolicy.isReadableSize(CatalogSnapshotPolicy.MAX_BYTES + 1))
        assertTrue(!CatalogSnapshotPolicy.shouldReadLegacy(metadataInstalled = true, size = 1L))
    }

    @Test
    fun `内置 seed 的 v3 快照被接受`() {
        val decoded = decodeSnapshot(snapshotJson(version = 3), CatalogOriginPolicy.SEED_ACCEPTED_VERSIONS)

        assertTrue("随包发布的 seed 仍是 v3，必须可读", decoded != null)
        assertEquals(listOf("年会不能停"), decoded!!.items.map { it.title })
        assertEquals("10000013", decoded.items.single().mytvId)
    }

    @Test
    fun `持久化快照仍严格拒绝 v3 接受 v4`() {
        assertTrue(
            "用户数据路径不能放宽：v3 持久化快照必须被拒绝",
            decodeSnapshot(snapshotJson(version = 3), CatalogOriginPolicy.PERSISTED_ACCEPTED_VERSIONS) == null,
        )
        assertTrue(
            "当前 v4 持久化快照必须可读",
            decodeSnapshot(snapshotJson(version = 4), CatalogOriginPolicy.PERSISTED_ACCEPTED_VERSIONS) != null,
        )
        assertEquals(setOf(3, 4), CatalogOriginPolicy.SEED_ACCEPTED_VERSIONS)
        assertEquals(setOf(4), CatalogOriginPolicy.PERSISTED_ACCEPTED_VERSIONS)
    }

    @Test
    fun `无 SQLite 时 v3 seed 驱动离线搜索与详情恢复`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val directory = File(context.filesDir, "metadata-catalog")
        directory.deleteRecursively()
        val snapshot = File(context.filesDir, "catalog_snapshot.json")
        snapshot.delete()
        val settings = SettingsStore(context)
        settings.setHhkanBase("")

        try {
            val repository = VodRepository(settings, OkHttpClient(), context)
            withTimeout(30_000L) { repository.awaitCatalogAvailable() }

            val match = repository.search("年会不能停").firstOrNull()
            assertTrue("v3 seed 必须能驱动离线搜索", match != null)
            val detail = repository.resolveDetail(match!!.id)
            assertEquals(match.title, detail?.title)
            assertEquals(match.mytvId, detail?.mytvId)
        } finally {
            directory.deleteRecursively()
            snapshot.delete()
        }
    }

    @Test
    fun `无SQLite首装种子不会把综艺热门和新上两栏折叠成一条`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val directory = File(context.filesDir, "metadata-catalog")
        directory.deleteRecursively()
        val snapshot = File(context.filesDir, "catalog_snapshot.json")
        snapshot.delete()
        val settings = SettingsStore(context)
        settings.setHhkanBase("")

        try {
            val repository = VodRepository(settings, OkHttpClient(), context)
            assertTrue(repository.awaitLibBlocksContent("douban:variety", budgetMs = 1_000L))

            val blocks = repository.libBlocksFlow.value[4].orEmpty()
            val hotIds = blocks[0].items.map { it.id }
            val newestIds = blocks[1].items.map { it.id }
            assertTrue("无 mytv_id 的豆瓣种子也必须保留多条综艺", hotIds.size > 1)
            assertTrue("最新上线不能只剩第一条种子", newestIds.size > 1)
            assertNotEquals("热门与最新栏不能复用同一顺序", hotIds, newestIds)
        } finally {
            directory.deleteRecursively()
            snapshot.delete()
        }
    }

    private fun snapshotJson(version: Int): String = """
        {
          "v": $version,
          "ts": 1790213591604,
          "categories": [{"id": "douban:movie", "name": "电影"}],
          "items": [{
            "id": "douban:1291546", "s": "douban", "t": "年会不能停", "c": "douban:movie",
            "y": "2021", "ar": "中国大陆", "r": 7.8, "p": "", "h": "", "rm": "TC",
            "d": "", "mytv_id": "10000013", "q": ["年会不能停", "nianhui"]
          }]
        }
    """.trimIndent()

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
    fun `搜索精确标题优先于高热度标题前缀`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val settings = SettingsStore(context)
        settings.setHhkanBase("")
        val store = MetadataCatalogStore(context, OkHttpClient(), BuildConfig.API_BASE_URL)
        val database = store.currentDatabaseFile
        database.parentFile?.deleteRecursively()
        database.parentFile?.mkdirs()
        writeExactPriorityCatalog(database)

        try {
            val repository = VodRepository(settings, OkHttpClient(), context)

            val ids = repository.search("霸王别姬").map { it.mytvId }
            assertEquals("精确标题必须排在高热度前缀与拼音命中前", "10000013", ids.first())
            assertTrue(ids.containsAll(listOf("10000014", "10000015")))
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

    @Test
    fun `豆瓣后台新榜单替换本地热门且好好看更新不覆盖榜单`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val directory = File(context.filesDir, "metadata-catalog")
        directory.deleteRecursively()
        val settings = SettingsStore(context)
        settings.setHhkanBase("http://catalog.test")
        val queries = mutableListOf<String?>()
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            val url = chain.request().url
            val body = if (url.encodedPath == "/vod/home") {
                queries += url.queryParameter("refresh")
                val fresh = url.queryParameter("refresh") == "1"
                val id = if (fresh) "42" else "41"
                """{"stale":${!fresh},"source":"douban","sections":[
                    {"title":"最近热门","items":[{"id":"$id","title":"豆瓣热门$id"}]},
                    {"title":"最新上映","items":[{"id":"43","title":"豆瓣新片"}]}
                ]}"""
            } else {
                """{"sections":[{"name":"最近更新","items":[{"id":901,"title":"真实更新"}]}]}"""
            }
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                .code(200).message("OK").body(body.toResponseBody("application/json".toMediaType())).build()
        }.build()
        try {
            val repository = VodRepository(settings, client, context)
            repository.awaitLibBlocks("douban:movie")
            val sections = repository.libBlocksFlow.value[1].orEmpty().associateBy { it.name }
            assertEquals(listOf(null, "1"), queries)
            assertEquals(listOf("douban:42"), sections.getValue("最近热门").items.map { it.id })
            assertEquals(listOf("douban:43"), sections.getValue("最新上线").items.map { it.id })
            assertEquals(listOf("hhkan:901"), sections.getValue("最近更新").items.map { it.id })
        } finally {
            directory.deleteRecursively()
        }
    }

    @Test
    fun `最近更新来自好好看分类真实更新栏`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val directory = File(context.filesDir, "metadata-catalog")
        directory.deleteRecursively()
        val snapshot = File(context.filesDir, "catalog_snapshot.json")
        snapshot.delete()
        val settings = SettingsStore(context)
        settings.setHhkanBase("http://catalog.test")
        val requestedPaths = mutableListOf<String>()
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            requestedPaths += chain.request().url.encodedPath
            Response.Builder()
                .request(chain.request())
                .protocol(Protocol.HTTP_1_1)
                .code(200)
                .message("OK")
                .body(
                    """{"sections":[
                        {"name":"最近热门","items":[{"id":101,"title":"接口热门"}]},
                        {"name":"最新上线","items":[{"id":102,"title":"接口新片"}]},
                        {"name":"最近更新","items":[
                            {"id":901,"title":"真实更新一"},
                            {"id":902,"title":"真实更新二"}
                        ]}
                    ]}""".toResponseBody("application/json".toMediaType()),
                )
                .build()
        }.build()

        try {
            val repository = VodRepository(settings, client, context)
            repository.awaitLibBlocks("douban:movie")

            val blocks = repository.libBlocksFlow.value[1].orEmpty().associateBy { it.name }
            assertEquals(listOf("/vod/home", "/hhkan/channel-sections/1"), requestedPaths)
            assertEquals(listOf("hhkan:901", "hhkan:902"), blocks.getValue("最近更新").items.map { it.id })
            assertEquals(listOf("真实更新一", "真实更新二"), blocks.getValue("最近更新").items.map { it.title })
        } finally {
            directory.deleteRecursively()
            snapshot.delete()
        }
    }

    @Test
    fun `最近上线保留新片排序而不是热门顺序`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val directory = File(context.filesDir, "metadata-catalog")
        directory.deleteRecursively()
        directory.mkdirs()
        writeInstalledMetadataDatabase(
            File(directory, "metadata.sqlite"),
            listOf(
                InstalledMedia(10000013L, "高热度影片", 2026, 100.0, 1_000L, "1291546"),
                InstalledMedia(10000014L, "新上线影片", 2026, 50.0, 1_000L, "1291547"),
            ),
        )
        val settings = SettingsStore(context)
        settings.setHhkanBase("")

        try {
            val repository = VodRepository(settings, OkHttpClient(), context)
            assertTrue(repository.awaitLibBlocksContent("douban:movie", budgetMs = 1_000L))

            val blocks = repository.libBlocksFlow.value[1].orEmpty()
            assertEquals(listOf("高热度影片", "新上线影片"), blocks[0].items.map { it.title })
            assertEquals(listOf("新上线影片", "高热度影片"), blocks[1].items.map { it.title })
        } finally {
            directory.deleteRecursively()
        }
    }

    @Test
    fun `装库后首页使用豆瓣榜单替换本地热门`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val directory = File(context.filesDir, "metadata-catalog")
        directory.deleteRecursively()
        directory.mkdirs()
        writeInstalledMetadataDatabase(File(directory, "metadata.sqlite"))
        val homeBody = """
            {"sections":[
                {"title":"最近热门","items":[
                    {"id":"1291546","title":"上游热门影片","rating":8.8,"mytv_id":"10000013"},
                    {"id":"1299999","title":"上游独家","rating":7.7,"mytv_id":"10009999"}
                ]},
                {"title":"上游新片","items":[
                    {"id":"1298888","title":"上游新片条目","rating":6.6,"mytv_id":"10008888"}
                ]}
            ],"stale":false,"source":"douban"}
        """.trimIndent().toByteArray()
        val server = StubServer(mapOf("/vod/home" to homeBody))
        server.start()
        val origin = server.origin
        val settings = SettingsStore(context)
        settings.setHhkanBase(origin)

        try {
            val repository = VodRepository(settings, OkHttpClient(), context)
            repository.refresh()
            assertTrue(repository.state.value.sourceStates.any { it.id == "metadata" })

            var guard = 0
            while (server.counts["/vod/home"] == 0 && guard++ < 50) kotlinx.coroutines.delay(20)
            repository.awaitHome()
            val home = repository.homeFlow.value

            assertTrue("已装库时也必须请求上游首页", (server.counts["/vod/home"] ?: 0) > 0)
            assertTrue("热门栏必须存在", home.sections.any { it.name == "最近热门" })
            assertTrue("上游板块必须并入", home.sections.any { it.name == "上游新片" })
            assertTrue(
                "上游同名板块完全替换本地热门且保持榜单顺序",
                home.sections.first { it.name == "最近热门" }.items.map { it.title } ==
                    listOf("上游热门影片", "上游独家"),
            )
        } finally {
            server.stop()
            directory.deleteRecursively()
        }
    }

    @Test
    fun `装库后上游不可用时首页仍保留本地板块`() = runBlocking {
        val context: Context = RuntimeEnvironment.getApplication()
        val directory = File(context.filesDir, "metadata-catalog")
        directory.deleteRecursively()
        directory.mkdirs()
        writeInstalledMetadataDatabase(File(directory, "metadata.sqlite"))
        val server = StubServer(emptyMap(), homeStatus = 503)
        server.start()
        val settings = SettingsStore(context)
        settings.setHhkanBase(server.origin)

        try {
            val repository = VodRepository(settings, OkHttpClient(), context)
            repository.refresh()
            repository.awaitHome()
            val home = repository.homeFlow.value

            assertTrue("上游失败也不能清空本地首页", home.sections.any { it.name == "最近热门" })
            assertTrue(!home.loading)
        } finally {
            server.stop()
            directory.deleteRecursively()
        }
    }

    /** Minimal loopback HTTP stub; the Android unit-test classpath has no com.sun.net.httpserver. */
    private class StubServer(
        private val bodies: Map<String, ByteArray>,
        private val homeStatus: Int = 200,
    ) {
        private lateinit var socket: java.net.ServerSocket
        @Volatile private var running = false
        val counts = java.util.concurrent.ConcurrentHashMap<String, Int>()
        lateinit var origin: String
            private set

        fun start() {
            socket = java.net.ServerSocket(0, 10, java.net.InetAddress.getByName("127.0.0.1"))
            origin = "http://127.0.0.1:${socket.localPort}"
            running = true
            thread(isDaemon = true) {
                while (running) {
                    val client = runCatching { socket.accept() }.getOrNull() ?: break
                    thread(isDaemon = true) { serve(client) }
                }
            }
        }

        private fun serve(client: java.net.Socket) {
            client.use {
                val input = it.getInputStream().bufferedReader(Charsets.US_ASCII)
                val requestLine = input.readLine().orEmpty()
                val path = requestLine.split(' ').getOrNull(1)?.substringBefore('?').orEmpty()
                while (input.readLine()?.isNotEmpty() == true) Unit
                counts.merge(path, 1, Int::plus)
                val status = if (path == "/vod/home") homeStatus else if (bodies.containsKey(path)) 200 else 404
                val body = bodies[path] ?: ByteArray(0)
                val output = it.getOutputStream()
                output.write(
                    "HTTP/1.1 $status ${if (status == 200) "OK" else "Unavailable"}\r\n".toByteArray(Charsets.US_ASCII)
                )
                if (status == 200) {
                    output.write("Content-Length: ${body.size}\r\n".toByteArray(Charsets.US_ASCII))
                }
                output.write("Connection: close\r\n\r\n".toByteArray(Charsets.US_ASCII))
                if (status == 200) output.write(body)
                output.flush()
            }
        }

        fun stop() {
            running = false
            runCatching { socket.close() }
        }
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

    private fun writeExactPriorityCatalog(file: File) {
        val db = SQLiteDatabase.openOrCreateDatabase(file, null)
        db.execSQL("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("INSERT INTO meta VALUES ('schema_version', '3')")
        db.execSQL("INSERT INTO meta VALUES ('data_version', '2026092401')")
        db.execSQL("CREATE TABLE media (id INTEGER PRIMARY KEY, media_type TEXT, title TEXT, original_title TEXT, year INTEGER, overview TEXT, popularity REAL, updated_at INTEGER)")
        db.execSQL("INSERT INTO media VALUES (10000013, 'movie', '霸王别姬', '', 1993, '', 5.0, 0)")
        db.execSQL("INSERT INTO media VALUES (10000014, 'movie', '霸王别姬外传', '', 1994, '', 999.0, 0)")
        db.execSQL("INSERT INTO media VALUES (10000015, 'movie', '别的影片', '', 1995, '', 5000.0, 0)")
        db.execSQL("CREATE TABLE external_id (media_id INTEGER, provider TEXT, external_id TEXT)")
        db.execSQL("CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT)")
        db.execSQL("CREATE TABLE credit (media_id INTEGER, person_id INTEGER, department TEXT, credit_order INTEGER)")
        db.execSQL("CREATE TABLE genre (id INTEGER PRIMARY KEY, name TEXT)")
        db.execSQL("CREATE TABLE media_genre (media_id INTEGER, genre_id INTEGER)")
        db.execSQL("CREATE TABLE region (id INTEGER PRIMARY KEY, name TEXT)")
        db.execSQL("CREATE TABLE media_region (media_id INTEGER, region_id INTEGER)")
        db.execSQL("CREATE TABLE rating (media_id INTEGER, provider TEXT, score REAL)")
        db.execSQL("CREATE TABLE image (media_id INTEGER, image_type TEXT, original_url TEXT)")
        db.execSQL("CREATE TABLE search_doc (media_id INTEGER, title_norm TEXT, aliases_norm TEXT, pinyin TEXT, pinyin_alts TEXT, initials TEXT, initials_alts TEXT, people_norm TEXT, people_pinyin TEXT, genres_norm TEXT, popularity REAL)")
        db.execSQL("INSERT INTO search_doc VALUES (10000013, '霸王别姬', '', '', '', '', '', '', '', '', 5.0)")
        db.execSQL("INSERT INTO search_doc VALUES (10000014, '霸王别姬外传', '', '', '', '', '', '', '', '', 999.0)")
        db.execSQL("INSERT INTO search_doc VALUES (10000015, '别的影片', '', '霸王别姬', '', '', '', '', '', '', 5000.0)")
        runCatching {
            db.execSQL("CREATE VIRTUAL TABLE search_fts USING fts5(media_id UNINDEXED, title, aliases, pinyin, initials, people, genres, prefix='2 3 4')")
            db.execSQL("INSERT INTO search_fts VALUES ('10000013', '霸王别姬', '', '', '', '', '')")
            db.execSQL("INSERT INTO search_fts VALUES ('10000014', '霸王别姬外传', '', '', '', '', '')")
            db.execSQL("INSERT INTO search_fts VALUES ('10000015', '别的影片', '', '霸王别姬', '', '', '')")
        }
        db.close()
    }

    private fun writeValidEmptyMetadataDatabase(file: File) {
        val db = SQLiteDatabase.openOrCreateDatabase(file, null)
        db.execSQL("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("INSERT INTO meta VALUES ('schema_version', '3')")
        db.execSQL("INSERT INTO meta VALUES ('data_version', '2026092401')")
        db.execSQL("CREATE TABLE media (id INTEGER PRIMARY KEY, media_type TEXT, title TEXT, original_title TEXT, year INTEGER, overview TEXT, popularity REAL, updated_at INTEGER)")
        db.execSQL("CREATE TABLE image (media_id INTEGER, image_type TEXT, original_url TEXT)")
        db.execSQL("CREATE TABLE rating (media_id INTEGER, provider TEXT, score REAL)")
        db.execSQL("CREATE TABLE external_id (media_id INTEGER, provider TEXT, external_id TEXT)")
        db.execSQL("CREATE TABLE genre (id INTEGER PRIMARY KEY, name TEXT)")
        db.execSQL("CREATE TABLE media_genre (media_id INTEGER, genre_id INTEGER)")
        db.execSQL("CREATE TABLE region (id INTEGER PRIMARY KEY, name TEXT)")
        db.execSQL("CREATE TABLE media_region (media_id INTEGER, region_id INTEGER)")
        db.execSQL("CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT)")
        db.execSQL("CREATE TABLE credit (media_id INTEGER, person_id INTEGER, department TEXT, credit_order INTEGER)")
        db.execSQL("CREATE TABLE search_doc (media_id INTEGER, title_norm TEXT, aliases_norm TEXT, pinyin TEXT, pinyin_alts TEXT, initials TEXT, initials_alts TEXT, people_norm TEXT, people_pinyin TEXT, genres_norm TEXT, popularity REAL)")
        db.close()
    }

    private data class InstalledMedia(
        val id: Long,
        val title: String,
        val year: Int,
        val popularity: Double,
        val updatedAt: Long,
        val externalId: String,
    )

    private fun writeInstalledMetadataDatabase(
        file: File,
        media: List<InstalledMedia> = listOf(
            InstalledMedia(10000013L, "本地影片", 1993, 5.0, 0L, "1291546"),
        ),
    ) {
        val db = SQLiteDatabase.openOrCreateDatabase(file, null)
        db.execSQL("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("INSERT INTO meta VALUES ('schema_version', '3')")
        db.execSQL("INSERT INTO meta VALUES ('data_version', '2026092401')")
        db.execSQL("CREATE TABLE media (id INTEGER PRIMARY KEY, media_type TEXT, title TEXT, original_title TEXT, year INTEGER, runtime INTEGER, overview TEXT, status TEXT, language TEXT, country TEXT, release_date TEXT, episodes INTEGER, season INTEGER, popularity REAL, created_at INTEGER, updated_at INTEGER)")
        media.forEach { row ->
            db.execSQL(
                "INSERT INTO media VALUES (?, 'movie', ?, '', ?, 100, '', 'released', 'zh', '中国大陆', '', NULL, NULL, ?, 0, ?)",
                arrayOf(row.id, row.title, row.year, row.popularity, row.updatedAt),
            )
        }
        db.execSQL("CREATE TABLE external_id (media_id INTEGER, provider TEXT, external_id TEXT, PRIMARY KEY(provider, external_id))")
        media.forEach { row ->
            db.execSQL("INSERT INTO external_id VALUES (?, 'douban', ?)", arrayOf(row.id, row.externalId))
        }
        db.execSQL("CREATE TABLE media_title (id INTEGER PRIMARY KEY, media_id INTEGER, title TEXT, language TEXT, title_type TEXT, normalized TEXT, pinyin TEXT, pinyin_alts TEXT, pinyin_initials TEXT, pinyin_initials_alts TEXT)")
        media.forEachIndexed { index, row ->
            db.execSQL(
                "INSERT INTO media_title VALUES (?, ?, ?, 'zh', 'primary', ?, '', '', '', '')",
                arrayOf(index + 1, row.id, row.title, row.title),
            )
        }
        db.execSQL("CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT NOT NULL, original_name TEXT, profile_url TEXT)")
        db.execSQL("CREATE TABLE credit (media_id INTEGER, person_id INTEGER, department TEXT, role TEXT, character_name TEXT, credit_order INTEGER, PRIMARY KEY(media_id, person_id, department, role))")
        db.execSQL("CREATE TABLE genre (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
        db.execSQL("INSERT INTO genre VALUES (1, '剧情')")
        db.execSQL("CREATE TABLE media_genre (media_id INTEGER, genre_id INTEGER, PRIMARY KEY(media_id, genre_id))")
        media.forEach { row -> db.execSQL("INSERT INTO media_genre VALUES (?, 1)", arrayOf(row.id)) }
        db.execSQL("CREATE TABLE region (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
        db.execSQL("INSERT INTO region VALUES (1, '中国大陆')")
        db.execSQL("CREATE TABLE media_region (media_id INTEGER, region_id INTEGER, PRIMARY KEY(media_id, region_id))")
        media.forEach { row -> db.execSQL("INSERT INTO media_region VALUES (?, 1)", arrayOf(row.id)) }
        db.execSQL("CREATE TABLE rating (media_id INTEGER, provider TEXT, score REAL, vote_count INTEGER, updated_at INTEGER, PRIMARY KEY(media_id, provider))")
        media.forEach { row -> db.execSQL("INSERT INTO rating VALUES (?, 'douban', 9.6, 1000, 0)", arrayOf(row.id)) }
        db.execSQL("CREATE TABLE image (id INTEGER PRIMARY KEY, media_id INTEGER, image_type TEXT, original_url TEXT, cached_url TEXT, width INTEGER, height INTEGER, UNIQUE(media_id, image_type))")
        db.execSQL("CREATE TABLE search_doc (media_id INTEGER PRIMARY KEY, title_norm TEXT, aliases_norm TEXT, pinyin TEXT, pinyin_alts TEXT, initials TEXT, initials_alts TEXT, people_norm TEXT, people_pinyin TEXT, genres_norm TEXT, popularity REAL, year INTEGER)")
        media.forEach { row ->
            db.execSQL(
                "INSERT INTO search_doc VALUES (?, ?, '', '', '', '', '', '', '', '剧情', ?, ?)",
                arrayOf(row.id, row.title, row.popularity, row.year),
            )
        }
        runCatching {
            db.execSQL("CREATE VIRTUAL TABLE search_fts USING fts5(media_id UNINDEXED, title, aliases, pinyin, initials, people, genres, prefix='2 3 4')")
            media.forEach { row ->
                db.execSQL("INSERT INTO search_fts VALUES (?, ?, '', '', '', '', '剧情')", arrayOf(row.id.toString(), row.title))
            }
        }
        db.close()
    }
}

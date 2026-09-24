package com.qiubo.optimaltv.data.catalog

import android.database.sqlite.SQLiteDatabase
import com.qiubo.optimaltv.data.model.CatalogMedia
import com.qiubo.optimaltv.data.source.VodApiSource
import java.io.ByteArrayOutputStream
import java.io.File
import java.net.ServerSocket
import java.net.Socket
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger
import java.util.zip.GZIPOutputStream
import kotlin.concurrent.thread
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.json.JSONArray
import org.json.JSONObject

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class MetadataCatalogStoreTest {
    @get:Rule
    val temporaryFolder = TemporaryFolder()

    private lateinit var server: ServerSocket
    private lateinit var origin: String
    @Volatile private var serverRunning = false
    private var acceptThread: Thread? = null
    private val replies = ConcurrentHashMap<String, Reply>()
    private val requestCounts = ConcurrentHashMap<String, AtomicInteger>()

    private data class Reply(
        val status: Int,
        val body: ByteArray,
        val declaredLength: Long = body.size.toLong(),
        val delayMillis: Long = 0,
    )

    @org.junit.Before
    fun startServer() {
        server = ServerSocket(0, 10, java.net.InetAddress.getByName("127.0.0.1"))
        serverRunning = true
        acceptThread = thread(isDaemon = true) {
            while (serverRunning) {
                val socket = runCatching { server.accept() }.getOrNull() ?: break
                thread(isDaemon = true) { serve(socket) }
            }
        }
        origin = "http://127.0.0.1:${server.localPort}"
        replies["/metadata/manifest.json"] = Reply(200, manifest("2026092401", full = fullEntry(gzip(emptyDatabase("2026092401")))).toByteArray())
    }

    @After
    fun stopServer() {
        serverRunning = false
        if (::server.isInitialized) server.close()
        acceptThread?.join(250)
    }

    private fun serve(socket: Socket) {
        socket.use { client ->
            val input = client.getInputStream().bufferedReader(Charsets.US_ASCII)
            val requestLine = input.readLine().orEmpty()
            val path = requestLine.split(' ').getOrNull(1)?.substringBefore('?').orEmpty()
            while (input.readLine()?.isNotEmpty() == true) Unit
            requestCounts.computeIfAbsent(path) { AtomicInteger() }.incrementAndGet()
            val reply = replies[path]
            if ((reply?.delayMillis ?: 0L) > 0) Thread.sleep(reply!!.delayMillis)
            val status = reply?.status ?: 404
            val body = reply?.body ?: ByteArray(0)
            val declaredLength = reply?.declaredLength ?: 0L
            val output = client.getOutputStream()
            output.write("HTTP/1.1 $status ${if (status == 200) "OK" else "Unavailable"}\r\n".toByteArray(Charsets.US_ASCII))
            output.write("Content-Length: $declaredLength\r\nConnection: close\r\n\r\n".toByteArray(Charsets.US_ASCII))
            output.write(body)
            output.flush()
        }
    }

    @Test
    fun `首装完整包安装后可按 mytv id 搜索并查询详情和豆瓣映射`() = runBlocking {
        val db = catalogDatabase("2026092401", title = "霸王别姬")
        publishFull("2026092401", db)
        val store = newStore()

        val result = store.refreshIfNeeded()

        assertTrue(result is CatalogRefreshResult.Installed)
        val searched = store.search("bwbj")
        assertEquals(listOf("10000013"), searched.map(CatalogMedia::mytvId))
        val detail = store.findByMyTvId("10000013")
        assertNotNull(detail)
        assertEquals("霸王别姬", detail?.title)
        assertEquals("douban:1291546", detail?.legacyRef)
        assertEquals("10000013", store.findByLegacyDoubanId("1291546")?.mytvId)
        val item = VodApiSource.fromCatalogMedia(detail!!)
        assertEquals("douban:1291546", item.id)
        assertEquals("10000013", item.mytvId)
    }

    @Test
    fun `同一 data version 不重复下载全量 SQLite`() = runBlocking {
        val db = catalogDatabase("2026092401")
        publishFull("2026092401", db)
        val store = newStore()

        store.refreshIfNeeded()
        val packageRequests = count("/v1/storages/object/hotupdate/metadata/full-2026092401.sqlite.gz")
        val result = store.refreshIfNeeded()

        assertTrue(result is CatalogRefreshResult.UpToDate)
        assertEquals(packageRequests, count("/v1/storages/object/hotupdate/metadata/full-2026092401.sqlite.gz"))
    }

    @Test
    fun `已安装目录的普通读取跳过全库完整性扫描 强制恢复仍执行扫描`() = runBlocking {
        val integrityChecks = AtomicInteger()
        val store = newStore(integrityCheck = { db ->
            integrityChecks.incrementAndGet()
            db.rawQuery("PRAGMA integrity_check", null).use { cursor ->
                if (cursor.moveToFirst()) cursor.getString(0) else ""
            }
        })
        publishFull("2026092401", catalogDatabase("2026092401"))

        store.refreshIfNeeded()
        val packageValidationChecks = integrityChecks.get()
        assertTrue("安装全量包时必须执行完整性校验", packageValidationChecks > 0)

        repeat(3) { assertTrue(store.hasInstalledDatabase()) }
        assertEquals("重复判断可用性不应反复扫描全库", packageValidationChecks, integrityChecks.get())

        val forced = store.refreshIfNeeded(force = true)
        assertTrue(forced is CatalogRefreshResult.UpToDate)
        assertEquals("明确强制恢复路径仍需完整性校验", packageValidationChecks + 1, integrityChecks.get())
    }

    @Test
    fun `强制刷新发现当前目录损坏后用已验证全量包恢复`() = runBlocking {
        val store = newStore()
        publishFull("2026092401", catalogDatabase("2026092401", title = "旧目录"))
        store.refreshIfNeeded()
        store.currentDatabaseFile.writeBytes("damaged sqlite".toByteArray())
        publishFull("2026092402", catalogDatabase("2026092402", title = "恢复目录"))

        val result = store.refreshIfNeeded(force = true)

        assertTrue(result is CatalogRefreshResult.Installed)
        assertEquals("恢复目录", store.findByMyTvId("10000013")?.title)
        assertEquals("ok", integrityCheck(store.currentDatabaseFile))
    }

    @Test
    fun `下载中断时旧目录字节和查询结果保持不变`() = runBlocking {
        val original = catalogDatabase("2026092401", title = "旧目录")
        publishFull("2026092401", original)
        val store = newStore()
        store.refreshIfNeeded()
        val oldBytes = store.currentDatabaseFile.readBytes()

        val next = catalogDatabase("2026092402", title = "新目录")
        publishFull("2026092402", next, interrupted = true)
        val result = store.refreshIfNeeded(force = true)

        assertTrue(result is CatalogRefreshResult.Failed)
        assertArrayEquals(oldBytes, store.currentDatabaseFile.readBytes())
        assertEquals("旧目录", store.findByMyTvId("10000013")?.title)
    }

    @Test
    fun `包大小或 SHA256 不匹配时拒绝安装并保留旧目录`() = runBlocking {
        val original = catalogDatabase("2026092401", title = "旧目录")
        publishFull("2026092401", original)
        val store = newStore()
        store.refreshIfNeeded()
        val oldBytes = store.currentDatabaseFile.readBytes()

        val next = catalogDatabase("2026092402", title = "新目录")
        publishFull("2026092402", next, wrongSize = true)
        val sizeResult = store.refreshIfNeeded(force = true)
        assertTrue(sizeResult is CatalogRefreshResult.Failed)
        assertArrayEquals(oldBytes, store.currentDatabaseFile.readBytes())

        publishFull("2026092403", catalogDatabase("2026092403", title = "再次更新"), wrongHash = true)
        val hashResult = store.refreshIfNeeded(force = true)
        assertTrue(hashResult is CatalogRefreshResult.Failed)
        assertArrayEquals(oldBytes, store.currentDatabaseFile.readBytes())
        assertEquals("旧目录", store.search("旧目录").single().title)
    }

    @Test
    fun `manifest schema 不支持时不下载也不替换当前目录`() = runBlocking {
        val original = catalogDatabase("2026092401", title = "旧目录")
        publishFull("2026092401", original)
        val store = newStore()
        store.refreshIfNeeded()
        val oldBytes = store.currentDatabaseFile.readBytes()
        replies["/metadata/manifest.json"] = Reply(
            200,
            manifest("2026092402", full = fullEntry(gzip(catalogDatabase("2026092402"))), schema = 4).toByteArray(),
        )

        val result = store.refreshIfNeeded(force = true)

        assertTrue(result is CatalogRefreshResult.Failed)
        assertArrayEquals(oldBytes, store.currentDatabaseFile.readBytes())
        assertEquals(0, count("/v1/storages/object/hotupdate/metadata/full-2026092402.sqlite.gz"))
    }

    @Test
    fun `包内 schema 或 data version 与 manifest 不一致时拒绝`() = runBlocking {
        val original = catalogDatabase("2026092401", title = "旧目录")
        publishFull("2026092401", original)
        val store = newStore()
        store.refreshIfNeeded()
        val oldBytes = store.currentDatabaseFile.readBytes()

        val unsupportedSchema = gzip(catalogDatabase("2026092402", schemaVersion = "2"))
        publishFull("2026092402", unsupportedSchema, databaseAlreadyGzipped = true)
        val schemaResult = store.refreshIfNeeded(force = true)
        assertTrue(schemaResult is CatalogRefreshResult.Failed)
        assertArrayEquals(oldBytes, store.currentDatabaseFile.readBytes())

        val wrongDataVersion = catalogDatabase("2026092401", title = "错误版本")
        publishFull("2026092402", wrongDataVersion)
        val dataVersionResult = store.refreshIfNeeded(force = true)
        assertTrue(dataVersionResult is CatalogRefreshResult.Failed)
        assertArrayEquals(oldBytes, store.currentDatabaseFile.readBytes())
    }

    @Test
    fun `integrity check 失败时保留此前可读目录`() = runBlocking {
        val original = catalogDatabase("2026092401", title = "旧目录")
        publishFull("2026092401", original)
        val store = newStore()
        store.refreshIfNeeded()
        val oldBytes = store.currentDatabaseFile.readBytes()

        publishFull("2026092402", "not a sqlite database".toByteArray())
        val result = store.refreshIfNeeded(force = true)

        assertTrue(result is CatalogRefreshResult.Failed)
        assertArrayEquals(oldBytes, store.currentDatabaseFile.readBytes())
        assertEquals("旧目录", store.findByMyTvId("10000013")?.title)
    }

    @Test
    fun `可用 delta 在副本事务中更新并原子替换目录`() = runBlocking {
        val original = catalogDatabase("2026092401", title = "旧目录")
        publishFull("2026092401", original)
        val store = newStore()
        store.refreshIfNeeded()
        val oldBytes = store.currentDatabaseFile.readBytes()
        val delta = gzip(
            """
            PRAGMA foreign_keys = OFF;
            BEGIN IMMEDIATE;
            UPDATE media SET title = '新目录' WHERE id = 10000013;
            UPDATE search_doc SET title_norm = '新目录' WHERE media_id = 10000013;
            UPDATE meta SET value = '2026092402' WHERE key = 'data_version';
            COMMIT;
            """.trimIndent().toByteArray(),
        )
        publishDelta("2026092401", "2026092402", delta)

        val result = store.refreshIfNeeded(force = true)

        assertTrue(result is CatalogRefreshResult.Installed)
        assertEquals("新目录", store.findByMyTvId("10000013")?.title)
        assertFalse(oldBytes.contentEquals(store.currentDatabaseFile.readBytes()))
        assertEquals("ok", integrityCheck(store.currentDatabaseFile))
    }

    @Test
    fun `from to 不适用的 delta 退回完整包`() = runBlocking {
        publishFull("2026092401", catalogDatabase("2026092401", title = "旧目录"))
        val store = newStore()
        store.refreshIfNeeded()

        val fullV2 = catalogDatabase("2026092402", title = "完整新目录")
        val gzipDelta = gzip("SELECT 1;".toByteArray())
        val full = fullEntry(gzip(fullV2), "2026092402")
        val delta = deltaEntry("2026092399", "2026092402", gzipDelta)
        replies["/metadata/manifest.json"] = Reply(200, manifest("2026092402", full, listOf(delta)).toByteArray())
        replies["/v1/storages/object/hotupdate/metadata/full-2026092402.sqlite.gz"] = Reply(200, gzip(fullV2))

        val result = store.refreshIfNeeded(force = true)

        assertTrue(result is CatalogRefreshResult.Installed)
        assertEquals(0, count("/v1/storages/object/hotupdate/metadata/delta-2026092399-2026092402.sql.gz"))
        assertEquals("完整新目录", store.findByMyTvId("10000013")?.title)
    }

    @Test
    fun `delta SQL 失败且全量回退失败时旧目录逐字节保留`() = runBlocking {
        val original = catalogDatabase("2026092401", title = "旧目录")
        publishFull("2026092401", original)
        val store = newStore()
        store.refreshIfNeeded()
        val oldBytes = store.currentDatabaseFile.readBytes()
        val badDelta = gzip(
            """
            BEGIN IMMEDIATE;
            UPDATE media SET title = '半成品' WHERE id = 10000013;
            INSERT INTO table_that_does_not_exist(value) VALUES ('fail');
            UPDATE meta SET value = '2026092402' WHERE key = 'data_version';
            COMMIT;
            """.trimIndent().toByteArray(),
        )
        publishDelta("2026092401", "2026092402", badDelta)
        replies["/v1/storages/object/hotupdate/metadata/full-2026092402.sqlite.gz"] = Reply(503, ByteArray(0))

        val result = store.refreshIfNeeded(force = true)

        assertTrue(result is CatalogRefreshResult.Failed)
        assertArrayEquals(oldBytes, store.currentDatabaseFile.readBytes())
        assertEquals("旧目录", store.findByMyTvId("10000013")?.title)
    }

    @Test
    fun `损坏的 delta 压缩包不会替换旧目录`() = runBlocking {
        val original = catalogDatabase("2026092401", title = "旧目录")
        publishFull("2026092401", original)
        val store = newStore()
        store.refreshIfNeeded()
        val oldBytes = store.currentDatabaseFile.readBytes()
        val malformed = "not a gzip SQL delta".toByteArray()
        publishDelta("2026092401", "2026092402", malformed)
        replies["/v1/storages/object/hotupdate/metadata/full-2026092402.sqlite.gz"] = Reply(503, ByteArray(0))

        val result = store.refreshIfNeeded(force = true)

        assertTrue(result is CatalogRefreshResult.Failed)
        assertArrayEquals(oldBytes, store.currentDatabaseFile.readBytes())
        assertEquals("旧目录", store.findByMyTvId("10000013")?.title)
    }

    @Test
    fun `并发刷新只下载一次完整目录`() = runBlocking {
        publishFull("2026092401", catalogDatabase("2026092401"), packageDelayMillis = 100)
        val store = newStore()

        val results = listOf(
            async(Dispatchers.IO) { store.refreshIfNeeded() },
            async(Dispatchers.IO) { store.refreshIfNeeded() },
        ).awaitAll()

        assertTrue(results.all { it is CatalogRefreshResult.Installed || it is CatalogRefreshResult.UpToDate })
        assertEquals(1, count("/v1/storages/object/hotupdate/metadata/full-2026092401.sqlite.gz"))
        assertEquals("霸王别姬", store.findByMyTvId("10000013")?.title)
    }

    @Test
    fun `无有效 SQLite 时以 APK seed 目录继续支持搜索和 legacy 映射`() = runBlocking {
        val seed = """
            {"v":3,"categories":[{"id":"douban:movie","name":"电影"}],"items":[
              {"id":"douban:seed-1","s":"douban","t":"离线种子片","c":"douban:movie","y":"2024","ar":"中国","r":8.2,"p":"https://img.example/poster.jpg","h":"","rm":"","d":"seed-1","mytv_id":"10000077"}
            ]}
        """.trimIndent()
        val store = newStore(seedCatalogJson = seed)
        replies["/metadata/manifest.json"] = Reply(503, ByteArray(0))

        val result = store.refreshIfNeeded()

        assertTrue(result is CatalogRefreshResult.Failed)
        assertEquals("10000077", store.search("离线种子片").single().mytvId)
        assertEquals("10000077", store.findByLegacyDoubanId("seed-1")?.mytvId)
    }

    @Test
    fun `FTS 与 schema LIKE fallback 都能返回目录结果`() = runBlocking {
        val ftsStore = installStore("2026092401", createFts = true)
        val fts = ftsStore.search("bawang")
        assertEquals("10000013", fts.firstOrNull()?.mytvId)

        val fallbackStore = installStore("2026092402", createFts = false)
        val fallback = fallbackStore.search("bwbj")
        assertEquals("10000013", fallback.firstOrNull()?.mytvId)
    }

    @Test
    fun `读取目录记录保留最近更新时间`() = runBlocking {
        val store = installStore("2026092403", updatedAt = 1_758_700_800L)

        assertEquals(1_758_700_800L, store.findByMyTvId("10000013")?.updatedAt)
    }

    @Test
    fun `替换目录不触碰 Room 收藏历史和进度记录`() = runBlocking {
        publishFull("2026092401", catalogDatabase("2026092401", title = "旧目录"))
        val store = newStore()
        store.refreshIfNeeded()
        val userDatabase = File(temporaryFolder.root, "user-data.sqlite")
        createUserData(userDatabase)
        val before = userRows(userDatabase)

        publishFull("2026092402", catalogDatabase("2026092402", title = "更新目录"))
        assertTrue(store.refreshIfNeeded(force = true) is CatalogRefreshResult.Installed)

        assertEquals(before, userRows(userDatabase))
        assertTrue(before["favorite"]?.firstOrNull().orEmpty().startsWith("douban:1291546|"))
        assertTrue(before["history"]?.firstOrNull().orEmpty().startsWith("douban:1291546|"))
        assertTrue(before["progress"]?.firstOrNull().orEmpty().startsWith("douban:1291546|"))
    }

    private fun installStore(
        version: String,
        createFts: Boolean = true,
        updatedAt: Long = 0L,
    ): MetadataCatalogStore {
        val store = newStore()
        publishFull(version, catalogDatabase(version, createFts = createFts, updatedAt = updatedAt))
        runBlocking { store.refreshIfNeeded() }
        return store
    }

    private fun newStore(
        seedCatalogJson: String = "",
        integrityCheck: ((SQLiteDatabase) -> String)? = null,
    ): MetadataCatalogStore = MetadataCatalogStore(
        File(temporaryFolder.root, "store-${System.nanoTime()}"),
        okhttp3.OkHttpClient(),
        origin,
        fullIntegrityCheck = integrityCheck ?: { db ->
            db.rawQuery("PRAGMA integrity_check", null).use { cursor ->
                if (cursor.moveToFirst()) cursor.getString(0) else ""
            }
        },
    ) { seedCatalogJson }

    private fun publishFull(
        version: String,
        database: ByteArray,
        databaseAlreadyGzipped: Boolean = false,
        interrupted: Boolean = false,
        wrongHash: Boolean = false,
        wrongSize: Boolean = false,
        packageDelayMillis: Long = 0,
    ) {
        val compressed = if (databaseAlreadyGzipped) database else gzip(database)
        val entry = fullEntry(compressed, version, wrongHash = wrongHash, wrongSize = wrongSize)
        replies["/metadata/manifest.json"] = Reply(200, manifest(version, entry).toByteArray())
        replies["/v1/storages/object/hotupdate/metadata/full-$version.sqlite.gz"] = Reply(
            status = 200,
            body = compressed,
            declaredLength = if (interrupted) compressed.size + 100L else compressed.size.toLong(),
            delayMillis = packageDelayMillis,
        )
    }

    private fun publishDelta(from: String, to: String, compressedDelta: ByteArray) {
        val fullDatabase = catalogDatabase(to, title = "全量回退目录")
        val full = fullEntry(gzip(fullDatabase), to)
        val delta = deltaEntry(from, to, compressedDelta)
        replies["/metadata/manifest.json"] = Reply(200, manifest(to, full, listOf(delta)).toByteArray())
        replies["/v1/storages/object/hotupdate/metadata/delta-$from-$to.sql.gz"] = Reply(200, compressedDelta)
        replies["/v1/storages/object/hotupdate/metadata/full-$to.sqlite.gz"] = Reply(200, gzip(fullDatabase))
    }

    private fun fullEntry(
        compressed: ByteArray,
        version: String = "2026092401",
        wrongHash: Boolean = false,
        wrongSize: Boolean = false,
    ) = JSONObject()
        .put("version", version)
        .put("url", "$origin/v1/storages/object/hotupdate/metadata/full-$version.sqlite.gz")
        .put("file", "full-$version.sqlite.gz")
        .put("sha256", if (wrongHash) "0".repeat(64) else sha256(compressed))
        .put("size", compressed.size + if (wrongSize) 1 else 0)
        .put("compression", "gzip")
        .put("uncompressed_sha256", sha256(runCatching { gunzip(compressed) }.getOrDefault(ByteArray(0))))
        .put("uncompressed_size", runCatching { gunzip(compressed).size }.getOrDefault(0))

    private fun deltaEntry(from: String, to: String, compressed: ByteArray) = JSONObject()
        .put("from", from)
        .put("to", to)
        .put("url", "$origin/v1/storages/object/hotupdate/metadata/delta-$from-$to.sql.gz")
        .put("file", "delta-$from-$to.sql.gz")
        .put("sha256", sha256(compressed))
        .put("size", compressed.size)
        .put("compression", "gzip")

    private fun manifest(version: String, full: JSONObject, deltas: List<JSONObject> = emptyList(), schema: Int = 3): String = JSONObject()
        .put("schema", schema)
        .put("latest_version", version)
        .put("full", full)
        .put("deltas", JSONArray(deltas))
        .toString()

    private fun catalogDatabase(
        version: String,
        title: String = "霸王别姬",
        schemaVersion: String = "3",
        createFts: Boolean = true,
        updatedAt: Long = 0L,
    ): ByteArray {
        val file = File(temporaryFolder.root, "fixture-${System.nanoTime()}.sqlite")
        val db = SQLiteDatabase.openOrCreateDatabase(file, null)
        db.execSQL("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("INSERT INTO meta VALUES ('schema_version', ?)", arrayOf(schemaVersion))
        db.execSQL("INSERT INTO meta VALUES ('data_version', ?)", arrayOf(version))
        db.execSQL("CREATE TABLE media (id INTEGER PRIMARY KEY, media_type TEXT, title TEXT, original_title TEXT, year INTEGER, runtime INTEGER, overview TEXT, status TEXT, language TEXT, country TEXT, release_date TEXT, episodes INTEGER, season INTEGER, popularity REAL, created_at INTEGER, updated_at INTEGER)")
        db.execSQL("INSERT INTO media VALUES (10000013, 'movie', ?, 'Farewell My Concubine', 1993, 171, '京剧演员的故事', 'released', 'zh', '中国大陆', '1993-01-01', NULL, NULL, 9.0, 0, ?)", arrayOf<Any>(title, updatedAt))
        db.execSQL("CREATE TABLE external_id (media_id INTEGER, provider TEXT, external_id TEXT, PRIMARY KEY(provider, external_id))")
        db.execSQL("INSERT INTO external_id VALUES (10000013, 'douban', '1291546')")
        db.execSQL("CREATE TABLE media_title (id INTEGER PRIMARY KEY, media_id INTEGER, title TEXT, language TEXT, title_type TEXT, normalized TEXT, pinyin TEXT, pinyin_alts TEXT, pinyin_initials TEXT, pinyin_initials_alts TEXT)")
        db.execSQL("INSERT INTO media_title VALUES (1, 10000013, ?, 'zh', 'primary', '霸王别姬', 'bawangbieji', '', 'bwbj', '')", arrayOf(title))
        db.execSQL("CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT NOT NULL, original_name TEXT, profile_url TEXT)")
        db.execSQL("INSERT INTO person VALUES (1, '陈凯歌', NULL, NULL)")
        db.execSQL("CREATE TABLE credit (media_id INTEGER, person_id INTEGER, department TEXT, role TEXT, character_name TEXT, credit_order INTEGER, PRIMARY KEY(media_id, person_id, department, role))")
        db.execSQL("INSERT INTO credit VALUES (10000013, 1, 'directing', 'director', NULL, 1)")
        db.execSQL("CREATE TABLE genre (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
        db.execSQL("INSERT INTO genre VALUES (1, '剧情')")
        db.execSQL("CREATE TABLE media_genre (media_id INTEGER, genre_id INTEGER, PRIMARY KEY(media_id, genre_id))")
        db.execSQL("INSERT INTO media_genre VALUES (10000013, 1)")
        db.execSQL("CREATE TABLE region (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
        db.execSQL("INSERT INTO region VALUES (1, '中国大陆')")
        db.execSQL("CREATE TABLE media_region (media_id INTEGER, region_id INTEGER, PRIMARY KEY(media_id, region_id))")
        db.execSQL("INSERT INTO media_region VALUES (10000013, 1)")
        db.execSQL("CREATE TABLE rating (media_id INTEGER, provider TEXT, score REAL, vote_count INTEGER, updated_at INTEGER, PRIMARY KEY(media_id, provider))")
        db.execSQL("INSERT INTO rating VALUES (10000013, 'douban', 9.6, 1000, 0)")
        db.execSQL("CREATE TABLE image (id INTEGER PRIMARY KEY, media_id INTEGER, image_type TEXT, original_url TEXT, cached_url TEXT, width INTEGER, height INTEGER, UNIQUE(media_id, image_type))")
        db.execSQL("INSERT INTO image VALUES (1, 10000013, 'poster', 'https://img.example/poster.jpg', NULL, 300, 450)")
        db.execSQL("CREATE TABLE search_doc (media_id INTEGER PRIMARY KEY, title_norm TEXT, aliases_norm TEXT, pinyin TEXT, pinyin_alts TEXT, initials TEXT, initials_alts TEXT, people_norm TEXT, people_pinyin TEXT, genres_norm TEXT, popularity REAL, year INTEGER)")
        db.execSQL("INSERT INTO search_doc VALUES (10000013, ?, '', 'bawangbieji', '', 'bwbj', '', '陈凯歌', 'chenkaige', '剧情', 9.0, 1993)", arrayOf(title))
        if (createFts) {
            runCatching {
                db.execSQL("CREATE VIRTUAL TABLE search_fts USING fts5(media_id UNINDEXED, title, aliases, pinyin, initials, people, genres, prefix='2 3 4')")
                db.execSQL("INSERT INTO search_fts VALUES ('10000013', ?, '', 'bawangbieji', 'bwbj', 'chenkaige', '剧情')", arrayOf(title))
            }
        }
        db.close()
        return file.readBytes()
    }

    private fun emptyDatabase(version: String): ByteArray = catalogDatabase(version)

    private fun createUserData(file: File) {
        val db = SQLiteDatabase.openOrCreateDatabase(file, null)
        db.execSQL("CREATE TABLE favorite (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("CREATE TABLE history (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("CREATE TABLE progress (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("INSERT INTO favorite VALUES ('douban:1291546', 'favorite')")
        db.execSQL("INSERT INTO history VALUES ('douban:1291546', '2026-09-24')")
        db.execSQL("INSERT INTO progress VALUES ('douban:1291546', 'episode=3;position=5000')")
        db.close()
    }

    private fun userRows(file: File): Map<String, List<String>> {
        val db = SQLiteDatabase.openDatabase(file.path, null, SQLiteDatabase.OPEN_READONLY)
        return try {
            listOf("favorite", "history", "progress").associateWith { table ->
                db.rawQuery("SELECT id, value FROM $table ORDER BY id", null).use { cursor ->
                    buildList { while (cursor.moveToNext()) add(cursor.getString(0) + "|" + cursor.getString(1)) }
                }
            }
        } finally {
            db.close()
        }
    }

    private fun integrityCheck(file: File): String {
        val db = SQLiteDatabase.openDatabase(file.path, null, SQLiteDatabase.OPEN_READONLY)
        return try {
            db.rawQuery("PRAGMA integrity_check", null).use { cursor ->
                if (cursor.moveToFirst()) cursor.getString(0) else ""
            }
        } finally {
            db.close()
        }
    }

    private fun count(path: String): Int = requestCounts[path]?.get() ?: 0

    private fun gzip(bytes: ByteArray): ByteArray = ByteArrayOutputStream().also { output ->
        GZIPOutputStream(output).use { it.write(bytes) }
    }.toByteArray()

    private fun gunzip(bytes: ByteArray): ByteArray = java.util.zip.GZIPInputStream(bytes.inputStream()).use { it.readBytes() }

    private fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes).joinToString("") { "%02x".format(it) }
}

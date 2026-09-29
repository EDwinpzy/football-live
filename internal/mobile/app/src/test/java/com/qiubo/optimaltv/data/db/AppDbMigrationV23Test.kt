package com.qiubo.optimaltv.data.db

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import androidx.room.Room
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import java.io.File

/**
 * v2 -> v3 迁移测试（BUG-03）。
 *
 * 背景：TV 端在 commit 587a621 给 [LastGoodSourceEntity] 加了 [LastGoodSourceEntity.lineIndex]
 * 与 [MIGRATION_2_3]，移动端漏同步，`AppDb` 停在 version = 2。缺这一段时读回会被当成
 * 第 0 条线路，于是「上次播第 3 条」静默变成「这次播第 0 条」——记忆指向错误线路，
 * 比没有记忆更糟（TV 端源码原注释）。
 *
 * 走**真实迁移路径**：先用原生 SQLite 造一个 schema 与 v2 逐字一致的库并写入用户记忆行，
 * 再用正式的 [AppDb]（version 3 + [MIGRATION_1_2, MIGRATION_2_3]）打开它触发迁移。
 */
@RunWith(RobolectricTestRunner::class)
class AppDbMigrationV23Test {

    private val context: Context = RuntimeEnvironment.getApplication()
    private var openDatabase: AppDb? = null

    @Before
    fun setUp() {
        context.deleteDatabase(DATABASE_NAME)
    }

    @After
    fun tearDown() {
        runCatching { openDatabase?.close() }
        context.deleteDatabase(DATABASE_NAME)
    }

    @Test
    fun `v2 migration adds the line index column and keeps old rows readable`() = runBlocking {
        createV2Database()

        val database = newDatabase()
        openDatabase = database
        val migrated = database.openHelper.writableDatabase
        assertEquals(3, migrated.version)

        // 迁移后 last_good_source 必须带上 line_index 列。
        val columns = mutableSetOf<String>()
        migrated.query("PRAGMA table_info(last_good_source)").use { cursor ->
            val nameColumn = cursor.getColumnIndexOrThrow("name")
            while (cursor.moveToNext()) columns += cursor.getString(nameColumn)
        }
        assertEquals(
            "v2 -> v3 迁移后 last_good_source 应含 line_index 列",
            setOf("mytv_id", "episode_index", "source_id", "content_id", "line_index", "successful_at"),
            columns,
        )

        // 旧行必须仍在：v2 写入时线路号就已丢失，无法还原，一律补 0
        // （「回落到第 0 条线路」= 旧行为），不做猜测性迁移。
        val kept = database.lastGoodSourceDao().get(7L, 2)
        assertNotNull("v2 已有的记忆行不能在迁移中丢失", kept)
        assertEquals("source-x", kept?.sourceId)
        assertEquals("content-y", kept?.contentId)
        assertEquals(0, kept?.lineIndex)
        assertEquals(1_758_000_000L, kept?.successfulAt)
    }

    @Test
    fun `v2 migration keeps other user tables intact`() = runBlocking {
        createV2Database()

        val database = newDatabase()
        openDatabase = database
        val dao = database.vodDao()

        // 收藏 / 历史 / 进度一条都不能少：迁移只应加一列。
        assertEquals(1, dao.favoritesFlow().first().size)
        val history = dao.historyFlow(limit = 50).first()
        assertEquals(1, history.size)
        assertEquals(4, history[0].epIndex)
        assertEquals(4_200L, history[0].positionMs)
        val progress = dao.getProgress("legacy-progress-1-e2")
        assertNotNull("观看进度在迁移中丢失", progress)
        assertEquals(7_200L, progress!!.positionMs)
    }

    @Test
    fun `after migration a new row round trips its real line index`() = runBlocking {
        createV2Database()

        val database = newDatabase()
        openDatabase = database

        // 新写入的行能带上真实线路号并原样读回——这是 BUG-03 的核心。
        val fresh = LastGoodSourceEntity(
            mytvId = 8L,
            episodeIndex = 1,
            sourceId = "source-a",
            contentId = "content-9",
            lineIndex = 5,
            successfulAt = 1_758_000_999L,
        )
        database.lastGoodSourceDao().upsert(fresh)
        val readBack = database.lastGoodSourceDao().get(8L, 1)
        assertNotNull(readBack)
        assertEquals(5, readBack!!.lineIndex)
        assertEquals(fresh, readBack)
    }

    @Test
    fun `v1 straight to v3 applies both migrations`() = runBlocking {
        // v1 的库没有 last_good_source 表，MIGRATION_1_2 建表、MIGRATION_2_3 再加列。
        context.deleteDatabase(DATABASE_NAME)
        val raw = SQLiteDatabase.openOrCreateDatabase(databaseFile(), null)
        try {
            raw.execSQL("CREATE TABLE favorite (vodId TEXT NOT NULL, title TEXT NOT NULL, posterUrl TEXT NOT NULL, createdAt INTEGER NOT NULL, PRIMARY KEY(vodId))")
            raw.execSQL("CREATE TABLE history (vodId TEXT NOT NULL, title TEXT NOT NULL, posterUrl TEXT NOT NULL, epIndex INTEGER NOT NULL, positionMs INTEGER NOT NULL, durationMs INTEGER NOT NULL, updatedAt INTEGER NOT NULL, PRIMARY KEY(vodId))")
            raw.execSQL("CREATE TABLE progress (vodKey TEXT NOT NULL, vodId TEXT NOT NULL, epIndex INTEGER NOT NULL, positionMs INTEGER NOT NULL, durationMs INTEGER NOT NULL, updatedAt INTEGER NOT NULL, PRIMARY KEY(vodKey))")
            raw.execSQL("CREATE TABLE room_master_table (id INTEGER PRIMARY KEY, identity_hash TEXT)")
            raw.execSQL("INSERT INTO room_master_table (id, identity_hash) VALUES (42, 'v1-fixture')")
            raw.version = 1
        } finally {
            raw.close()
        }

        val database = newDatabase()
        openDatabase = database
        assertEquals(3, database.openHelper.writableDatabase.version)

        val fresh = LastGoodSourceEntity(1L, 0, "hhkan", "abc", 3, 7L)
        database.lastGoodSourceDao().upsert(fresh)
        assertEquals(fresh, database.lastGoodSourceDao().get(1L, 0))
    }

    private fun newDatabase(): AppDb =
        Room.databaseBuilder(context, AppDb::class.java, DATABASE_NAME)
            .addMigrations(MIGRATION_1_2, MIGRATION_2_3)
            .allowMainThreadQueries()
            .build()

    /** 构造一个 v2 库：三张用户表 + 没有 line_index 的 last_good_source（含一行真实记忆）。 */
    private fun createV2Database() {
        val file = databaseFile()
        file.parentFile?.mkdirs()
        context.deleteDatabase(DATABASE_NAME)
        val raw = SQLiteDatabase.openOrCreateDatabase(file, null)
        try {
            raw.execSQL("CREATE TABLE favorite (vodId TEXT NOT NULL, title TEXT NOT NULL, posterUrl TEXT NOT NULL, createdAt INTEGER NOT NULL, PRIMARY KEY(vodId))")
            raw.execSQL("CREATE TABLE history (vodId TEXT NOT NULL, title TEXT NOT NULL, posterUrl TEXT NOT NULL, epIndex INTEGER NOT NULL, positionMs INTEGER NOT NULL, durationMs INTEGER NOT NULL, updatedAt INTEGER NOT NULL, PRIMARY KEY(vodId))")
            raw.execSQL("CREATE TABLE progress (vodKey TEXT NOT NULL, vodId TEXT NOT NULL, epIndex INTEGER NOT NULL, positionMs INTEGER NOT NULL, durationMs INTEGER NOT NULL, updatedAt INTEGER NOT NULL, PRIMARY KEY(vodKey))")
            raw.execSQL(
                """
                CREATE TABLE last_good_source (
                    mytv_id INTEGER NOT NULL,
                    episode_index INTEGER NOT NULL,
                    source_id TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    successful_at INTEGER NOT NULL,
                    PRIMARY KEY(mytv_id, episode_index)
                )
                """.trimIndent(),
            )
            raw.execSQL("CREATE TABLE room_master_table (id INTEGER PRIMARY KEY, identity_hash TEXT)")
            raw.execSQL("INSERT INTO room_master_table (id, identity_hash) VALUES (42, 'v2-fixture')")
            raw.execSQL(
                "INSERT INTO last_good_source (mytv_id, episode_index, source_id, content_id, successful_at) VALUES (?, ?, ?, ?, ?)",
                arrayOf<Any>(7L, 2, "source-x", "content-y", 1_758_000_000L),
            )
            raw.execSQL(
                "INSERT INTO favorite (vodId, title, posterUrl, createdAt) VALUES (?, ?, ?, ?)",
                arrayOf<Any>("douban:old-1", "霸王别姬", "https://img/1", 100L),
            )
            raw.execSQL(
                "INSERT INTO history (vodId, title, posterUrl, epIndex, positionMs, durationMs, updatedAt) VALUES (?, ?, ?, ?, ?, ?, ?)",
                arrayOf<Any>("legacy-history-1", "肖申克的救赎", "https://img/h1", 4, 4_200L, 9_000L, 400L),
            )
            raw.execSQL(
                "INSERT INTO progress (vodKey, vodId, epIndex, positionMs, durationMs, updatedAt) VALUES (?, ?, ?, ?, ?, ?)",
                arrayOf<Any>("legacy-progress-1-e2", "legacy-progress-1", 2, 7_200L, 12_000L, 600L),
            )
            raw.version = 2
        } finally {
            raw.close()
        }
    }

    private fun databaseFile(): File = context.getDatabasePath(DATABASE_NAME)

    private companion object {
        const val DATABASE_NAME = "appdb-migration-v23-test.db"
    }
}

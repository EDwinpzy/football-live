package com.qiubo.optimaltv.data.db

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import androidx.room.Room
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import java.io.File

@RunWith(RobolectricTestRunner::class)
class AppDbMigrationTest {
    private val context: Context = RuntimeEnvironment.getApplication()
    private var openDatabase: AppDb? = null

    @After
    fun tearDown() {
        openDatabase?.close()
        context.deleteDatabase(DATABASE_NAME)
    }

    @Test
    fun `v1 migration preserves user rows and creates private writable last good table`() = runBlocking {
        createV1Database()

        val database = newDatabase()
        openDatabase = database
        val migrated = database.openHelper.writableDatabase

        assertEquals(3, migrated.version)

        val expectedFavorites = listOf(
            FavoriteEntity("douban:old-2", "Favorite Two", "https://img/2", 200L),
            FavoriteEntity("douban:old-1", "Favorite One", "https://img/1", 100L),
        )
        val expectedHistory = listOf(
            HistoryEntity("legacy-history-2", "History Two", "https://img/h2", 4, 4_200L, 9_000L, 400L),
            HistoryEntity("legacy-history-1", "History One", "https://img/h1", 2, 1_500L, 8_000L, 300L),
        )
        val expectedProgress = listOf(
            ProgressEntity("legacy-progress-1-e2", "legacy-progress-1", 2, 7_200L, 12_000L, 600L),
            ProgressEntity("legacy-progress-2", "legacy-progress-2", 0, 3_300L, 10_000L, 500L),
        )

        val vodDao = database.vodDao()
        val favorites = vodDao.favoritesFlow().first()
        val history = vodDao.historyFlow(limit = 10).first()
        val progress = listOf(
            vodDao.getProgress("legacy-progress-1-e2"),
            vodDao.getProgress("legacy-progress-2"),
        )

        assertEquals(2, favorites.size)
        assertEquals(expectedFavorites, favorites)
        assertEquals(setOf("douban:old-1", "douban:old-2"), favorites.map { it.vodId }.toSet())
        assertEquals(2, history.size)
        assertEquals(expectedHistory, history)
        assertEquals(setOf("legacy-history-1", "legacy-history-2"), history.map { it.vodId }.toSet())
        assertEquals(2, progress.size)
        assertEquals(expectedProgress, progress)
        assertEquals(setOf("legacy-progress-1-e2", "legacy-progress-2"), progress.map { it?.vodKey }.toSet())

        val lastGoodColumns = mutableSetOf<String>()
        migrated.query("PRAGMA table_info(last_good_source)").use { cursor ->
            val nameColumn = cursor.getColumnIndexOrThrow("name")
            while (cursor.moveToNext()) lastGoodColumns += cursor.getString(nameColumn)
        }
        assertEquals(
            setOf("mytv_id", "episode_index", "source_id", "content_id", "line_index", "successful_at"),
            lastGoodColumns,
        )
        assertTrue(database.lastGoodSourceDao().all().isEmpty())

        val lastGood = LastGoodSourceEntity(
            mytvId = 8342L,
            episodeIndex = 3,
            sourceId = "source-a",
            contentId = "content-9",
            lineIndex = 2,
            successfulAt = 1_758_000_123L,
        )
        database.lastGoodSourceDao().upsert(lastGood)
        assertEquals(lastGood, database.lastGoodSourceDao().get(8342L, 3))
        assertEquals(listOf(lastGood), database.lastGoodSourceDao().all())
    }

    @Test
    fun `v2 migration adds the line index column and keeps old rows readable`() = runBlocking {
        createV2Database()

        val database = newDatabase()
        openDatabase = database
        val migrated = database.openHelper.writableDatabase
        assertEquals(3, migrated.version)

        // 旧行必须可读，且 line_index 补 0（写入时就丢了线路号，无法还原，
        // 保持「回落到第 0 条线路」= 旧行为，不做猜测性迁移）。
        val kept = database.lastGoodSourceDao().get(7L, 2)
        assertNotNull("v2 已有的记忆行不能在迁移中丢失", kept)
        assertEquals("source-x", kept?.sourceId)
        assertEquals("content-y", kept?.contentId)
        assertEquals(0, kept?.lineIndex)

        // 新写入的行能带上真实线路号并原样读回。
        val fresh = LastGoodSourceEntity(
            mytvId = 8L,
            episodeIndex = 1,
            sourceId = "source-a",
            contentId = "content-9",
            lineIndex = 5,
            successfulAt = 1_758_000_999L,
        )
        database.lastGoodSourceDao().upsert(fresh)
        assertEquals(fresh, database.lastGoodSourceDao().get(8L, 1))
    }

    @Test
    fun `unknown future database version fails without deleting user rows`() {
        createV1Database(version = 999)

        val database = newDatabase()
        openDatabase = database
        val failure = runCatching { database.openHelper.writableDatabase }.exceptionOrNull()

        assertNotNull("An unsupported future schema must surface as a recoverable open error", failure)
        assertTrue(failure is IllegalStateException)

        val raw = SQLiteDatabase.openDatabase(databaseFile().absolutePath, null, SQLiteDatabase.OPEN_READONLY)
        try {
            assertEquals(999, raw.version)
            assertEquals(2, rowCount(raw, "favorite"))
            assertEquals(2, rowCount(raw, "history"))
            assertEquals(2, rowCount(raw, "progress"))
        } finally {
            raw.close()
        }
    }

    private fun newDatabase(): AppDb = Room.databaseBuilder(context, AppDb::class.java, DATABASE_NAME)
        .addMigrations(MIGRATION_1_2, MIGRATION_2_3)
        .allowMainThreadQueries()
        .build()

    /** 构造一个 v2 库：v1 的三张用户表 + 没有 line_index 的 last_good_source。 */
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
            raw.version = 2
        } finally {
            raw.close()
        }
    }

    private fun createV1Database(version: Int = 1) {
        val file = databaseFile()
        file.parentFile?.mkdirs()
        context.deleteDatabase(DATABASE_NAME)
        val raw = SQLiteDatabase.openOrCreateDatabase(file, null)
        try {
            raw.execSQL("CREATE TABLE favorite (vodId TEXT NOT NULL, title TEXT NOT NULL, posterUrl TEXT NOT NULL, createdAt INTEGER NOT NULL, PRIMARY KEY(vodId))")
            raw.execSQL("CREATE TABLE history (vodId TEXT NOT NULL, title TEXT NOT NULL, posterUrl TEXT NOT NULL, epIndex INTEGER NOT NULL, positionMs INTEGER NOT NULL, durationMs INTEGER NOT NULL, updatedAt INTEGER NOT NULL, PRIMARY KEY(vodId))")
            raw.execSQL("CREATE TABLE progress (vodKey TEXT NOT NULL, vodId TEXT NOT NULL, epIndex INTEGER NOT NULL, positionMs INTEGER NOT NULL, durationMs INTEGER NOT NULL, updatedAt INTEGER NOT NULL, PRIMARY KEY(vodKey))")
            raw.execSQL("CREATE TABLE room_master_table (id INTEGER PRIMARY KEY, identity_hash TEXT)")
            raw.execSQL("INSERT INTO room_master_table (id, identity_hash) VALUES (42, 'v1-fixture')")

            raw.execSQL(
                "INSERT INTO favorite (vodId, title, posterUrl, createdAt) VALUES (?, ?, ?, ?)",
                arrayOf("douban:old-1", "Favorite One", "https://img/1", 100L),
            )
            raw.execSQL(
                "INSERT INTO favorite (vodId, title, posterUrl, createdAt) VALUES (?, ?, ?, ?)",
                arrayOf("douban:old-2", "Favorite Two", "https://img/2", 200L),
            )
            raw.execSQL(
                "INSERT INTO history (vodId, title, posterUrl, epIndex, positionMs, durationMs, updatedAt) VALUES (?, ?, ?, ?, ?, ?, ?)",
                arrayOf("legacy-history-1", "History One", "https://img/h1", 2, 1_500L, 8_000L, 300L),
            )
            raw.execSQL(
                "INSERT INTO history (vodId, title, posterUrl, epIndex, positionMs, durationMs, updatedAt) VALUES (?, ?, ?, ?, ?, ?, ?)",
                arrayOf("legacy-history-2", "History Two", "https://img/h2", 4, 4_200L, 9_000L, 400L),
            )
            raw.execSQL(
                "INSERT INTO progress (vodKey, vodId, epIndex, positionMs, durationMs, updatedAt) VALUES (?, ?, ?, ?, ?, ?)",
                arrayOf("legacy-progress-1-e2", "legacy-progress-1", 2, 7_200L, 12_000L, 600L),
            )
            raw.execSQL(
                "INSERT INTO progress (vodKey, vodId, epIndex, positionMs, durationMs, updatedAt) VALUES (?, ?, ?, ?, ?, ?)",
                arrayOf("legacy-progress-2", "legacy-progress-2", 0, 3_300L, 10_000L, 500L),
            )
            raw.execSQL("PRAGMA user_version = $version")
        } finally {
            raw.close()
        }
    }

    private fun databaseFile(): File = context.getDatabasePath(DATABASE_NAME)

    private fun rowCount(database: SQLiteDatabase, table: String): Int =
        database.rawQuery("SELECT COUNT(*) FROM $table", null).use { cursor ->
            cursor.moveToFirst()
            cursor.getInt(0)
        }

    private companion object {
        const val DATABASE_NAME = "appdb-migration-test.db"
    }
}

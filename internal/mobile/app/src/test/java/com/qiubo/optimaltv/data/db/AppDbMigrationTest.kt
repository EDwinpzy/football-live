package com.qiubo.optimaltv.data.db

import androidx.room.Room
import androidx.room.RoomDatabase
import java.io.File
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

/**
 * v1 -> v2 迁移测试。
 *
 * 走**真实迁移路径**：先用 Room 在测试源集里建一个 version=1 的库并写入用户数据
 * （LegacyV1Db，见本文件末尾），再用正式的 [AppDb]（version 2 + [MIGRATION_1_2]）打开它。
 * 不用手写 SQL 伪造 v1 库——那会绕过 Room 的 schema identity 校验，测不到迁移本身。
 * v2 -> v3 那一段另有 [AppDbMigrationV23Test] 覆盖。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class AppDbMigrationTest {

    @get:Rule
    val temporaryFolder = TemporaryFolder()

    private lateinit var dbFile: File
    private var legacy: RoomDatabase? = null
    private var migrated: AppDb? = null

    @Before
    fun setUp() {
        dbFile = File(temporaryFolder.root, "optimal_tv.db")
    }

    @After
    fun tearDown() {
        runCatching { migrated?.close() }
        runCatching { legacy?.close() }
    }

    /** 建一个真实的 v1 库并写入收藏 / 历史 / 进度三类用户数据。 */
    private fun seedV1WithUserData() = runBlocking {
        val db = Room.databaseBuilder(
            RuntimeEnvironment.getApplication(), LegacyV1Db::class.java, dbFile.absolutePath,
        ).allowMainThreadQueries().build()
        legacy = db
        val dao = db.vodDao()
        dao.insertFavorite(FavoriteEntity("douban:1291546", "霸王别姬", "p1", 100L))
        dao.insertFavorite(FavoriteEntity("mytv:10000001", "沙丘", "p2", 200L))
        dao.insertHistory(HistoryEntity("douban:1291546", "霸王别姬", "p1", 3, 5000L, 90000L, 300L))
        dao.insertProgress(ProgressEntity("douban:1291546-e3", "douban:1291546", 3, 5000L, 90000L, 400L))
        db.close()
        legacy = null
    }

    /** 用正式 AppDb 打开同一个文件，触发 v1 -> v2 迁移。 */
    private fun openMigrated(): AppDb =
        Room.databaseBuilder(
            RuntimeEnvironment.getApplication(), AppDb::class.java, dbFile.absolutePath,
        )
            // AppDb 已是 version 3；Room 需要直接的 1 -> 3 路径，故两个迁移都注册
            // （与 AppDb.get() 的注册口径一致）。
            .addMigrations(MIGRATION_1_2, MIGRATION_2_3)
            .allowMainThreadQueries()
            .build()
            .also { migrated = it }

    @Test
    fun migration1To2_createsLastGoodSourceTable() {
        seedV1WithUserData()
        val db = openMigrated()
        // 查 sqlite_master 验表真的建出来了，绕开 DAO 缓存造成的假阳性。
        db.openHelper.readableDatabase.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='last_good_source'",
        ).use { cursor ->
            assertTrue("last_good_source 表未建出", cursor.moveToFirst())
        }
    }

    @Test
    fun migration1To2_keepsExistingUserData() {
        seedV1WithUserData()
        val dao = openMigrated().vodDao()
        // 迁移必须非破坏性：收藏 / 历史 / 进度一条都不能少。
        val favorites = runBlocking { dao.favoritesFlow().first() }
        assertEquals(2, favorites.size)
        assertEquals("霸王别姬", favorites.first { it.vodId == "douban:1291546" }.title)
        assertEquals(100L, favorites.first { it.vodId == "douban:1291546" }.createdAt)

        val history = runBlocking { dao.historyFlow(50).first() }
        assertEquals(1, history.size)
        assertEquals(5000L, history[0].positionMs)
        assertEquals(3, history[0].epIndex)

        val progress = runBlocking { dao.getProgress("douban:1291546-e3") }
        assertNotNull("观看进度在迁移中丢失", progress)
        assertEquals(400L, progress!!.updatedAt)
        assertEquals(5_000L, progress.positionMs)
    }

    @Test
    fun migration1To2_preservesLastGoodSourceWhenTableAlreadyExists() = runBlocking {
        seedV1WithUserData()
        val db = openMigrated()
        db.lastGoodSourceDao().upsert(
            LastGoodSourceEntity(
                mytvId = 10000001L,
                episodeIndex = 0,
                sourceId = "hhkan",
                contentId = "abc123",
                successfulAt = 999L,
            ),
        )
        db.close()
        migrated = null

        // 再开一次：迁移里的 CREATE TABLE IF NOT EXISTS 不应清空已有行。
        val kept = openMigrated().lastGoodSourceDao().get(10000001L, 0)
        assertNotNull("已有的 last_good_source 行被迁移清空了", kept)
        assertEquals("abc123", kept!!.contentId)
    }

    @Test
    fun lastGoodSourceDao_roundTripsByCompositeKey() = runBlocking {
        seedV1WithUserData()
        val dao = openMigrated().lastGoodSourceDao()
        dao.upsert(LastGoodSourceEntity(10000001L, 2, "hhkan", "vid-1", 0, 10L))
        dao.upsert(LastGoodSourceEntity(10000001L, 3, "hhkan", "vid-2", 0, 20L))
        // 复合主键：同 (mytv_id, episode) 再写应覆盖而非新增
        dao.upsert(LastGoodSourceEntity(10000001L, 2, "hhkan", "vid-1-new", 0, 30L))
        assertEquals(2, dao.all().size)
        assertEquals("vid-1-new", dao.get(10000001L, 2)?.contentId)
        assertEquals("vid-2", dao.get(10000001L, 3)?.contentId)
        // 集号隔离：第 2 集的记录不能被第 9 集命中
        assertNull(dao.get(10000001L, 9))
        dao.delete(10000001L, 2)
        assertEquals(1, dao.all().size)
    }
}

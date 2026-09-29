package com.qiubo.optimaltv.data.db

import android.content.Context
import androidx.room.Dao
import androidx.room.ColumnInfo
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import kotlinx.coroutines.flow.Flow
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

/** 收藏表（技术方案 §3.5） */
@Entity(tableName = "favorite")
data class FavoriteEntity(
    @PrimaryKey val vodId: String,
    val title: String,
    val posterUrl: String,
    val createdAt: Long,
)

/** 观看历史表 */
@Entity(tableName = "history")
data class HistoryEntity(
    @PrimaryKey val vodId: String,
    val title: String,
    val posterUrl: String,
    val epIndex: Int,
    val positionMs: Long,
    val durationMs: Long,
    val updatedAt: Long,
)

/** 播放进度表：vodKey = 多集 "{vodId}-e{n}"，单集 "{vodId}" */
@Entity(tableName = "progress")
data class ProgressEntity(
    @PrimaryKey val vodKey: String,
    val vodId: String,
    val epIndex: Int,
    val positionMs: Long,
    val durationMs: Long,
    val updatedAt: Long,
)

/** 最近成功线路的稳定引用，不保存播放 URL、Cookie 或临时认证信息。 */
@Entity(
    tableName = "last_good_source",
    primaryKeys = ["mytv_id", "episode_index"],
)
data class LastGoodSourceEntity(
    @ColumnInfo(name = "mytv_id") val mytvId: Long,
    @ColumnInfo(name = "episode_index") val episodeIndex: Int,
    @ColumnInfo(name = "source_id") val sourceId: String,
    @ColumnInfo(name = "content_id") val contentId: String,
    /**
     * 该内容在源下的**线路号**（对应 line id 第三段），不是集号。
     * 缺这一段时读回会被当成第 0 条线路，于是「上次播第 3 条」变成「这次播第 0 条」——
     * 记忆静默指向错误线路，比没有记忆更糟。
     */
    @ColumnInfo(name = "line_index", defaultValue = "0") val lineIndex: Int = 0,
    @ColumnInfo(name = "successful_at") val successfulAt: Long,
)

@Dao
interface VodDao {

    // ---- favorite ----
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun addFavorite(item: FavoriteEntity)

    @Query("DELETE FROM favorite WHERE vodId = :vodId")
    suspend fun removeFavorite(vodId: String)

    @Query("SELECT * FROM favorite ORDER BY createdAt DESC")
    fun favoritesFlow(): Flow<List<FavoriteEntity>>

    @Query("SELECT EXISTS(SELECT 1 FROM favorite WHERE vodId = :vodId)")
    fun isFavoriteFlow(vodId: String): Flow<Boolean>

    @Query("DELETE FROM favorite")
    suspend fun clearFavorites()

    // ---- history ----
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertHistory(item: HistoryEntity)

    @Query("SELECT * FROM history ORDER BY updatedAt DESC LIMIT :limit")
    fun historyFlow(limit: Int = 30): Flow<List<HistoryEntity>>

    @Query("SELECT * FROM history WHERE vodId = :vodId LIMIT 1")
    suspend fun getHistory(vodId: String): HistoryEntity?

    @Query("DELETE FROM history")
    suspend fun clearHistory()

    // ---- progress ----
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertProgress(item: ProgressEntity)

    @Query("SELECT * FROM progress WHERE vodKey = :vodKey LIMIT 1")
    suspend fun getProgress(vodKey: String): ProgressEntity?

    /** 该影片全部线路/集的进度（详情页续播判定批量取数：旧版逐集 getProgress 串行上千次） */
    @Query("SELECT * FROM progress WHERE vodId = :vodId")
    suspend fun progressOfVod(vodId: String): List<ProgressEntity>

    @Query("DELETE FROM progress WHERE vodId = :vodId")
    suspend fun clearProgressOfVod(vodId: String)

    @Query("DELETE FROM progress")
    suspend fun clearAllProgress()
}

@Dao
interface LastGoodSourceDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(item: LastGoodSourceEntity)

    @Query("SELECT * FROM last_good_source WHERE mytv_id = :mytvId AND episode_index = :episodeIndex LIMIT 1")
    suspend fun get(mytvId: Long, episodeIndex: Int): LastGoodSourceEntity?

    @Query("SELECT * FROM last_good_source")
    suspend fun all(): List<LastGoodSourceEntity>

    @Query("DELETE FROM last_good_source WHERE mytv_id = :mytvId AND episode_index = :episodeIndex")
    suspend fun delete(mytvId: Long, episodeIndex: Int)
}

val MIGRATION_1_2 = object : Migration(1, 2) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL(
            """
                CREATE TABLE IF NOT EXISTS last_good_source (
                    mytv_id INTEGER NOT NULL,
                    episode_index INTEGER NOT NULL,
                    source_id TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    successful_at INTEGER NOT NULL,
                    PRIMARY KEY(mytv_id, episode_index)
                )
            """.trimIndent(),
        )
    }
}

/**
 * v2 → v3：last_good_source 补上 `line_index`。
 *
 * 缺这一段时读回会被当成第 0 条线路（见 [LastGoodSourceEntity.lineIndex]），
 * 于是「上次成功播第 3 条」会静默变成「这次播第 0 条」。
 *
 * 已有数据一律补 0：那批记录写入时就丢掉了线路号、无法还原，保持原语义
 * （「回落到该内容第 0 条线路」= 旧行为），不做猜测性迁移。
 */
val MIGRATION_2_3 = object : Migration(2, 3) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL("ALTER TABLE last_good_source ADD COLUMN line_index INTEGER NOT NULL DEFAULT 0")
    }
}

@Database(
    entities = [FavoriteEntity::class, HistoryEntity::class, ProgressEntity::class, LastGoodSourceEntity::class],
    version = 3,
    exportSchema = false,
)
abstract class AppDb : RoomDatabase() {
    abstract fun vodDao(): VodDao
    abstract fun lastGoodSourceDao(): LastGoodSourceDao

    companion object {
        @Volatile private var instance: AppDb? = null

        fun get(context: Context): AppDb = instance ?: synchronized(this) {
            instance ?: Room.databaseBuilder(context.applicationContext, AppDb::class.java, "optimal_tv.db")
                .addMigrations(MIGRATION_1_2, MIGRATION_2_3)
                .build()
                .also { instance = it }
        }
    }
}

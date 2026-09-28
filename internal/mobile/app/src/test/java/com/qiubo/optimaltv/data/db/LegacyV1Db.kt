package com.qiubo.optimaltv.data.db

import androidx.room.Dao
import androidx.room.Database
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.RoomDatabase

/**
 * 迁移前的 v1 库形态：只有收藏 / 历史 / 进度三张表。
 *
 * 只在测试源集里存在，用来真实地造出一个 version=1 的 SQLite 库，
 * 供 [AppDbMigrationTest] 走 v1 -> v2 的真实迁移路径。
 * 绝不能把它留在 main 源集——那会让 App 真的带着一个 v1 库上线。
 */
@Dao
interface LegacyV1Dao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertFavorite(item: FavoriteEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertHistory(item: HistoryEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertProgress(item: ProgressEntity)
}

@Database(
    entities = [FavoriteEntity::class, HistoryEntity::class, ProgressEntity::class],
    version = 1,
    exportSchema = false,
)
abstract class LegacyV1Db : RoomDatabase() {
    abstract fun vodDao(): LegacyV1Dao
}

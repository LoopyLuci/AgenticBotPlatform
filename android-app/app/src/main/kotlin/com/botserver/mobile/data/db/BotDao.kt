package com.botserver.mobile.data.db

import androidx.room.Dao
import androidx.room.Delete
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Transaction
import kotlinx.coroutines.flow.Flow

@Dao
interface BotDao {
    @Query("SELECT * FROM bots ORDER BY name COLLATE NOCASE")
    fun observeAll(): Flow<List<BotEntity>>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertAll(bots: List<BotEntity>)

    @Query("DELETE FROM bots WHERE id NOT IN (:keepIds)")
    suspend fun deleteMissing(keepIds: List<Int>)

    /** Replaces the whole cache with `bots` in one transaction — the
     * pragmatic choice for a list this small (tens of rows, not
     * thousands): simpler and just as correct as a diff-and-patch, since
     * every refresh() call already has the server's full, authoritative
     * list in hand. */
    @Transaction
    suspend fun replaceAll(bots: List<BotEntity>) {
        upsertAll(bots)
        deleteMissing(bots.map { it.id })
    }

    @Query("DELETE FROM bots WHERE id = :id")
    suspend fun deleteById(id: Int)
}

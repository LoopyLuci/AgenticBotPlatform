package com.botserver.mobile.data.db

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface ChatDao {
    // Most recent `limit` rows, re-ascending for display — a plain bounded
    // query rather than a full Paging 3 PagingSource: this app's chat
    // volume doesn't call for windowed loading of very old history yet,
    // and a bounded query gets the actual audited problem (an in-memory
    // list that grew forever for the life of the ViewModel) fixed with far
    // less surface area. Revisit with real Paging if a conversation
    // regularly needs to scroll back past this window.
    @Query(
        """
        SELECT * FROM (
            SELECT * FROM chat_messages WHERE instanceId = :instanceId ORDER BY id DESC LIMIT :limit
        ) ORDER BY id ASC
        """,
    )
    fun observeRecent(instanceId: Int, limit: Int = 300): Flow<List<ChatMessageEntity>>

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertAll(messages: List<ChatMessageEntity>)

    @Query("SELECT MAX(id) FROM chat_messages WHERE instanceId = :instanceId")
    suspend fun maxId(instanceId: Int): Int?

    @Query(
        """
        DELETE FROM chat_messages WHERE instanceId = :instanceId AND id NOT IN (
            SELECT id FROM chat_messages WHERE instanceId = :instanceId ORDER BY id DESC LIMIT :keep
        )
        """,
    )
    suspend fun pruneOld(instanceId: Int, keep: Int = 500)
}

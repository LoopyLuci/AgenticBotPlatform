package com.botserver.mobile.data.db

import androidx.room.Entity

/** Offline cache of GET /api/chat/messages, scoped per bot instance. Unlike
 * BotEntity, there's no secret here worth excluding — chat content is the
 * whole point of caching it (so a conversation is still visible offline or
 * after the app is killed, instead of resetting to blank). Room is also
 * what bounds memory growth now: ChatDao only ever observes the most
 * recent [ChatDao.observeRecent] rows and periodically prunes older ones,
 * replacing the old in-memory list that grew forever for the life of the
 * ViewModel. */
@Entity(tableName = "chat_messages", primaryKeys = ["instanceId", "id"])
data class ChatMessageEntity(
    val instanceId: Int,
    val id: Int,
    val ts: String,
    val platform: String,
    val chatId: String,
    val userId: String?,
    val username: String?,
    val direction: String,
    val source: String,
    val text: String,
    val attachmentPath: String?,
    val attachmentName: String?,
    val attachmentMime: String?,
    val attachmentSize: Long?,
    val thumbnailPath: String?,
)

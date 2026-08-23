package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Mirrors GET /api/chat/recipients — see bot/dashboard/server.py's
 * api_chat_recipients(). */
@Serializable
data class ChatRecipientsResponse(
    val instances: List<BotInstanceSummary> = emptyList(),
)

@Serializable
data class BotInstanceSummary(
    val id: Int,
    val name: String,
    val platform: String,
    @SerialName("allowed_ids") val allowedIds: List<String> = emptyList(),
    val connected: Boolean = false,
)

/** Mirrors one row from GET /api/chat/messages — see bot/db.py's messages
 * table / log_message(). */
@Serializable
data class ChatMessage(
    val id: Int,
    val ts: String,
    val platform: String,
    @SerialName("chat_id") val chatId: String,
    @SerialName("user_id") val userId: String? = null,
    val username: String? = null,
    val direction: String,
    val source: String,
    val text: String,
    @SerialName("instance_id") val instanceId: Int? = null,
    @SerialName("attachment_path") val attachmentPath: String? = null,
    @SerialName("attachment_name") val attachmentName: String? = null,
    @SerialName("attachment_mime") val attachmentMime: String? = null,
    @SerialName("attachment_size") val attachmentSize: Long? = null,
    @SerialName("thumbnail_path") val thumbnailPath: String? = null,
)

@Serializable
data class SendMessageRequest(
    @SerialName("instance_id") val instanceId: Int,
    @SerialName("chat_id") val chatId: String,
    val text: String,
)

@Serializable
data class OkResponse(val ok: Boolean = false)

/** Mirrors POST /api/chat/send-to-bot — "Chat with Bot" mode: a real
 * message to the bot, no chat_id needed (the server derives the sender's
 * identity from this request's own auth). */
@Serializable
data class SendToBotRequest(
    @SerialName("instance_id") val instanceId: Int,
    val text: String,
)

/** Response for send-to-bot — the bot's real reply text, returned directly
 * (in addition to it landing in the message log the next poll picks up) so
 * the UI can show it immediately. */
@Serializable
data class SendToBotResponse(
    val ok: Boolean = false,
    val reply: String? = null,
)

@Serializable
data class RegisterPushTokenRequest(@SerialName("fcm_token") val fcmToken: String)

/** Chunked-upload protocol — mirrors bot/dashboard/server.py's
 * /api/uploads/init|chunk|complete. init declares the file and gets back a
 * session id plus the chunk size to use; complete assembles + relays and
 * reports whether it actually went out through the bot (large files are
 * stored server-only, pull-based, if they exceed the platform's own relay
 * limit — see PLATFORM_RELAY_LIMIT_BYTES server-side). */
@Serializable
data class UploadInitRequest(
    @SerialName("instance_id") val instanceId: Int,
    @SerialName("chat_id") val chatId: String,
    val filename: String,
    @SerialName("total_size") val totalSize: Long,
    val mime: String? = null,
    val text: String = "",
)

@Serializable
data class UploadInitResponse(
    @SerialName("session_id") val sessionId: String,
    @SerialName("chunk_size") val chunkSize: Int,
)

@Serializable
data class UploadCompleteResponse(
    val ok: Boolean = false,
    val id: Int,
    val relayed: Boolean = true,
)

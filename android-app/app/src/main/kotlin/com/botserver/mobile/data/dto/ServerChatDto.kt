package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Mirrors GET /api/server-chat/conversations — see bot/db.py's
 * server_chat_conversations table and list_server_chat_conversations(). */
@Serializable
data class ServerChatConversation(
    val id: Int,
    val kind: String,   // "group" | "direct"
    val title: String,
    @SerialName("peer_device_id") val peerDeviceId: Int? = null,
    @SerialName("last_message") val lastMessage: ServerChatLastMessage? = null,
)

@Serializable
data class ServerChatLastMessage(
    val text: String? = null,
    val ts: String? = null,
    @SerialName("attachment_name") val attachmentName: String? = null,
)

/** Mirrors one row from GET /api/server-chat/messages —
 * bot/db.py's server_chat_messages table. `senderDeviceId` is 0 for the
 * desktop app, otherwise another paired device's own api_keys.id — never
 * this device's own id, since a device never sees its own outgoing message
 * labeled as anything but "this device is 0 vs not-0" from its own point
 * of view (see MY_DEVICE_ID resolution client-side: this device only knows
 * "mine" by comparing sender_device_id against its own /api/devices entry,
 * which ServerChatRepository resolves once and caches). */
@Serializable
data class ServerChatMessage(
    val id: Int,
    @SerialName("conversation_id") val conversationId: Int,
    @SerialName("sender_device_id") val senderDeviceId: Int,
    val ts: String,
    val text: String = "",
    @SerialName("attachment_path") val attachmentPath: String? = null,
    @SerialName("attachment_name") val attachmentName: String? = null,
    @SerialName("attachment_mime") val attachmentMime: String? = null,
    @SerialName("attachment_size") val attachmentSize: Long? = null,
)

@Serializable
data class ServerChatSendRequest(
    @SerialName("conversation_id") val conversationId: Int,
    val text: String,
)

@Serializable
data class ServerChatSendResponse(
    val ok: Boolean,
    val id: Int,
)

/** Mirrors GET /api/server-chat/whoami — resolves this device's own
 * Server Chat identity (0 for desktop, otherwise this device's own
 * api_keys.id) so the client can tell "sent by me" from "received". */
@Serializable
data class ServerChatWhoAmI(
    @SerialName("device_id") val deviceId: Int,
)

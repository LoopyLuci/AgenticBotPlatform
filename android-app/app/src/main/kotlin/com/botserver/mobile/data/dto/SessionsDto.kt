package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.jsonPrimitive

/** Mirrors GET /api/sessions rows — see bot/dashboard/server.py's
 * api_sessions(). `id` is a JsonElement, not Int, because legacy/pre-
 * sessions-feature buckets use a synthetic string id like "legacy-3"
 * (bot/db.py has no numeric id for those) alongside real integer ids for
 * actual sessions — sessionIdString() below normalizes both. */
@Serializable
data class SessionSummary(
    val id: JsonElement,
    @SerialName("instance_id") val instanceId: Int? = null,
    @SerialName("chat_id") val chatId: String? = null,
    val title: String = "",
    @SerialName("started_at") val startedAt: String? = null,
    @SerialName("last_activity_at") val lastActivityAt: String? = null,
    @SerialName("item_count") val itemCount: Int = 0,
    val legacy: Boolean = false,
) {
    fun sessionIdString(): String = id.jsonPrimitive.content
}

@Serializable
data class SessionDetail(
    val session: SessionSummary,
    val messages: List<ChatMessage> = emptyList(),
    val jobs: List<JobSummary> = emptyList(),
)

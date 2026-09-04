package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonPrimitive

/** Mirrors bot_instances.py's credentials JSON shape — bot_token for
 * Telegram/Discord, bot_token+app_token for Slack, homeserver/user_id/
 * access_token(/device_id) for Matrix, phone_number_id/access_token/
 * app_secret/verify_token for WhatsApp (see bot/validators.py's
 * PLATFORM_TOKEN_VALIDATORS for the authoritative per-platform field
 * list). Full parity with the desktop dashboard means this app can read
 * and write real platform credentials; see bot/dashboard/server.py's
 * _identify_caller() docstring for the tradeoff that was a deliberate
 * choice, not an oversight. */
@Serializable
data class BotCredentials(
    @SerialName("bot_token") val botToken: String? = null,
    @SerialName("app_token") val appToken: String? = null,
    val homeserver: String? = null,
    @SerialName("user_id") val userId: String? = null,
    @SerialName("access_token") val accessToken: String? = null,
    @SerialName("device_id") val deviceId: String? = null,
    @SerialName("phone_number_id") val phoneNumberId: String? = null,
    @SerialName("app_secret") val appSecret: String? = null,
    @SerialName("verify_token") val verifyToken: String? = null,
)

/** Mirrors one row from GET /api/bots — see bot/bot_instances.py's
 * _row_to_dict(). allowed_user_ids is numeric JSON for Telegram/Discord but
 * string JSON for Slack, so it's kept as a raw JsonArray and normalized to
 * strings via [allowedUserIds] rather than risking a deserialization
 * mismatch on one platform's shape. */
@Serializable
data class BotInstance(
    val id: Int,
    val name: String,
    val platform: String,
    val backend: String,
    val enabled: Boolean,
    val model: String? = null,
    val persona: String = "assistant",
    val credentials: BotCredentials = BotCredentials(),
    @SerialName("allowed_user_ids") val allowedUserIdsRaw: JsonArray = JsonArray(emptyList()),
    // Read-only here (deliberately absent from BotWriteRequest below, same
    // reasoning as can_target's omission there — see that field's comment):
    // this app can show who's an admin, but editing the tier list stays a
    // dashboard/desktop action so an unrelated mobile edit can never
    // silently wipe it via kotlinx's default-encoding of an omitted field.
    @SerialName("admin_user_ids") val adminUserIdsRaw: JsonArray = JsonArray(emptyList()),
    @SerialName("can_target") val canTarget: List<Int> = emptyList(),
    @SerialName("last_error") val lastError: String? = null,
    @SerialName("live_running") val liveRunning: Boolean = false,
) {
    val allowedUserIds: List<String>
        get() = allowedUserIdsRaw.map { it.jsonPrimitive.content }

    val adminUserIds: List<String>
        get() = adminUserIdsRaw.map { it.jsonPrimitive.content }
}

@Serializable
data class BotWriteRequest(
    val name: String,
    val platform: String,
    val backend: String,
    val model: String? = null,
    val persona: String? = null,
    val credentials: BotCredentials,
    @SerialName("allowed_user_ids") val allowedUserIds: List<String>,
    val enabled: Boolean = true,
)

/** Mirrors one row from GET /api/pairing — see bot/pairing.py and
 * bot/db.py's pairing_codes table. A pending request from an unrecognized
 * chat-platform sender, waiting for an admin to approve or deny it. */
@Serializable
data class PairingRequest(
    val id: Int,
    @SerialName("instance_id") val instanceId: Int,
    val code: String,
    @SerialName("user_id") val userId: String,
    @SerialName("user_name") val userName: String? = null,
    @SerialName("created_at") val createdAt: String,
    @SerialName("expires_at") val expiresAt: String,
)

@Serializable
data class PairingListResponse(val pending: List<PairingRequest> = emptyList())

/** Mirrors GET /api/personas — see bot/personas.py's PERSONA_PRESETS. */
@Serializable
data class PersonaPreset(
    val id: String,
    val label: String,
    val icon: String,
    val description: String,
)

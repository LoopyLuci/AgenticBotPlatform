package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonPrimitive

/** Mirrors bot_instances.py's credentials JSON shape — bot_token for every
 * platform, app_token additionally for Slack (xapp-...). Full parity with
 * the desktop dashboard means this app can read and write real platform
 * bot tokens; see bot/dashboard/server.py's _identify_caller() docstring
 * for the tradeoff that was a deliberate choice, not an oversight. */
@Serializable
data class BotCredentials(
    @SerialName("bot_token") val botToken: String? = null,
    @SerialName("app_token") val appToken: String? = null,
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
    @SerialName("can_target") val canTarget: List<Int> = emptyList(),
    @SerialName("last_error") val lastError: String? = null,
    @SerialName("live_running") val liveRunning: Boolean = false,
) {
    val allowedUserIds: List<String>
        get() = allowedUserIdsRaw.map { it.jsonPrimitive.content }
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

/** Mirrors GET /api/personas — see bot/personas.py's PERSONA_PRESETS. */
@Serializable
data class PersonaPreset(
    val id: String,
    val label: String,
    val icon: String,
    val description: String,
)

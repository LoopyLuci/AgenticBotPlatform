package com.botserver.mobile.data.dto

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull

/** Mirrors GET /api/config — see bot/dashboard/server.py's api_config().
 * `current` is config/backends.yaml's live in-memory shape, which is loose
 * and evolves outside this app's control, so it's kept as a raw JsonObject
 * with small typed accessors for just the settings this app's Settings
 * screen actually exposes (mirrors dashboard main.js's own reads of the
 * same fields — default_backend, agent_control.mode, feature toggles). */
@Serializable
data class ConfigResponse(
    val version: Int = 0,
    val current: JsonObject = JsonObject(emptyMap()),
)

private fun JsonObject.at(vararg path: String): JsonElement? {
    var node: JsonElement = this
    for (key in path) {
        node = (node as? JsonObject)?.get(key) ?: return null
    }
    return node
}

private fun JsonObject.stringAt(vararg path: String): String? =
    (at(*path) as? JsonPrimitive)?.takeIf { it != JsonNull }?.contentOrNull

private fun JsonObject.boolAt(vararg path: String): Boolean? =
    (at(*path) as? JsonPrimitive)?.booleanOrNull

val ConfigResponse.defaultBackend: String? get() = current.stringAt("default_backend")
val ConfigResponse.defaultHermesBackend: String? get() = current.stringAt("default_hermes_backend")
val ConfigResponse.agentControlMode: String? get() = current.stringAt("agent_control", "mode")
val ConfigResponse.uiAutomationEnabled: Boolean? get() = current.boolAt("features", "ui_automation_enabled")
val ConfigResponse.confirmDestructive: Boolean? get() = current.boolAt("security", "confirm_destructive")
val ConfigResponse.verboseTelemetry: Boolean? get() = current.boolAt("features", "verbose_telemetry")

/** Mirrors GET /api/models — see bot/dashboard/server.py's api_models()
 * and bot/models.py's KNOWN_MODELS. `known` only has a closed list for the
 * "api" backend; hermes_cli/hermes_gateway accept free-form model names. */
@Serializable
data class ModelsResponse(
    val known: Map<String, List<String>> = emptyMap(),
    val current: Map<String, String?> = emptyMap(),
)

/** Body for POST /api/config/set — {path: [...], value: ...}, same generic
 * shape the desktop dashboard already uses for every settings write. */
@Serializable
data class ConfigSetRequest(val path: List<String>, val value: JsonElement)

fun configSetRequest(path: List<String>, value: String?): ConfigSetRequest =
    ConfigSetRequest(path, JsonPrimitive(value))

fun configSetRequest(path: List<String>, value: Boolean): ConfigSetRequest =
    ConfigSetRequest(path, JsonPrimitive(value))

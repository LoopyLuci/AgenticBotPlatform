package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class SupportBotAskRequest(
    val text: String,
    // Optional local-classification fast-path hint (Phase 6 of the
    // Support Bot NLU upgrade plan, com.botserver.mobile.nlu) — the
    // server independently re-validates and gates every real action
    // regardless of this value; see bot/support_bot/engine.py's
    // handle() docstring for the exact guarantee.
    @SerialName("client_intent") val clientIntent: String? = null,
)

@Serializable
data class SupportBotConfirmRequest(val token: String)

/** Tier 1 of the Support Bot NLU cascade — request/response for
 * POST /api/support-bot/classify (next-generation modular hybrid plan,
 * Phase 4). Purely advisory: see SupportBotRepository.kt's own docstring
 * for why local/server classification never executes anything by
 * itself. */
@Serializable
data class SupportBotClassifyRequest(val text: String)

@Serializable
data class SupportBotClassifyResponse(
    val intent: String,
    val confidence: Double,
    val source: String,
    @SerialName("server_model_version") val serverModelVersion: String,
)

/** One Knowledge Module's entry from GET /api/support-bot/manifest —
 * mirrors bot/support_bot/knowledge_modules.py's ModuleSpec plus its
 * runtime state from module_manifest.py. */
@Serializable
data class SupportBotModuleInfo(
    @SerialName("display_name") val displayName: String,
    val description: String,
    val intents: List<String>,
    val unloadable: Boolean,
    val enabled: Boolean,
    val version: String? = null,
    @SerialName("updated_at") val updatedAt: String? = null,
)

/** Mirrors bot/support_bot/engine.py's SupportBotReply — the local,
 * dependency-free assistant built into the desktop server. Same shape the
 * desktop dashboard's Support Bot panel already consumes. */
@Serializable
data class SupportBotReply(
    val text: String,
    val intent: String? = null,
    @SerialName("needs_confirm") val needsConfirm: Boolean = false,
    @SerialName("confirm_token") val confirmToken: String? = null,
    val applied: Boolean = false,
)

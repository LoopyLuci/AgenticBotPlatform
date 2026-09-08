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

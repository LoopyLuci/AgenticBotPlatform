package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class SupportBotAskRequest(val text: String)

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

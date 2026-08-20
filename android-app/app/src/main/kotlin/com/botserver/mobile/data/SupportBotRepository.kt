package com.botserver.mobile.data

import com.botserver.mobile.data.dto.SupportBotAskRequest
import com.botserver.mobile.data.dto.SupportBotConfirmRequest
import com.botserver.mobile.data.dto.SupportBotReply
import javax.inject.Inject
import javax.inject.Singleton

/** Thin wrapper over the desktop server's local Support Bot — same
 * bot/support_bot/ classifier and management actions the desktop
 * dashboard's Support Bot panel talks to, just from the phone. */
@Singleton
class SupportBotRepository @Inject constructor(private val apiService: ApiService) {
    suspend fun ask(text: String): SupportBotReply = apiService.supportBotAsk(SupportBotAskRequest(text))

    suspend fun confirm(token: String): SupportBotReply = apiService.supportBotConfirm(SupportBotConfirmRequest(token))
}

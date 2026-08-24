package com.botserver.mobile.data

import com.botserver.mobile.data.dto.BotCredentials
import com.botserver.mobile.data.dto.BotInstance
import com.botserver.mobile.data.dto.BotWriteRequest
import com.botserver.mobile.data.dto.PairingRequest
import com.botserver.mobile.data.dto.PersonaPreset
import javax.inject.Inject
import javax.inject.Singleton

/** Full CRUD over bot instances — mirrors the desktop dashboard's Bots tab
 * exactly, including create/edit (which submits the platform bot token)
 * and delete. See ApiService.kt's comment on why these particular routes
 * are reachable from a mobile device key. */
@Singleton
class BotsRepository @Inject constructor(private val apiService: ApiService) {
    suspend fun list(): List<BotInstance> = apiService.bots()

    suspend fun get(id: Int): BotInstance = apiService.bot(id)

    suspend fun personas(): List<PersonaPreset> = apiService.personas()

    suspend fun create(
        name: String,
        platform: String,
        backend: String,
        model: String?,
        persona: String,
        botToken: String,
        appToken: String?,
        allowedUserIds: List<String>,
    ) {
        apiService.createBot(
            BotWriteRequest(
                name = name,
                platform = platform,
                backend = backend,
                model = model?.takeIf { it.isNotBlank() },
                persona = persona,
                credentials = BotCredentials(botToken = botToken, appToken = appToken?.takeIf { it.isNotBlank() }),
                allowedUserIds = allowedUserIds,
            ),
        )
    }

    suspend fun update(
        id: Int,
        name: String,
        platform: String,
        backend: String,
        model: String?,
        persona: String,
        botToken: String,
        appToken: String?,
        allowedUserIds: List<String>,
        enabled: Boolean,
    ) {
        apiService.updateBot(
            id,
            BotWriteRequest(
                name = name,
                platform = platform,
                backend = backend,
                model = model?.takeIf { it.isNotBlank() },
                persona = persona,
                credentials = BotCredentials(botToken = botToken, appToken = appToken?.takeIf { it.isNotBlank() }),
                allowedUserIds = allowedUserIds,
                enabled = enabled,
            ),
        )
    }

    suspend fun delete(id: Int) {
        apiService.deleteBot(id)
    }

    suspend fun restart(id: Int) {
        apiService.restartBot(id)
    }

    suspend fun toggleEnabled(bot: BotInstance) {
        if (bot.enabled) apiService.disableBot(bot.id) else apiService.enableBot(bot.id)
    }

    suspend fun toggleRunning(bot: BotInstance) {
        if (bot.liveRunning) apiService.stopBot(bot.id) else apiService.startBot(bot.id)
    }

    suspend fun pendingPairings(): List<PairingRequest> = apiService.pairing().pending

    suspend fun approvePairing(id: Int) {
        apiService.approvePairing(id)
    }

    suspend fun denyPairing(id: Int) {
        apiService.denyPairing(id)
    }
}

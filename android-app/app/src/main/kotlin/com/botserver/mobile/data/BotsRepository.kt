package com.botserver.mobile.data

import com.botserver.mobile.data.db.BotDao
import com.botserver.mobile.data.db.BotEntity
import com.botserver.mobile.data.dto.BotCredentials
import com.botserver.mobile.data.dto.BotInstance
import com.botserver.mobile.data.dto.BotWriteRequest
import com.botserver.mobile.data.dto.PairingRequest
import com.botserver.mobile.data.dto.PersonaPreset
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import javax.inject.Inject
import javax.inject.Singleton

/** Full CRUD over bot instances — mirrors the desktop dashboard's Bots tab
 * exactly, including create/edit (which submits the platform bot token)
 * and delete. See ApiService.kt's comment on why these particular routes
 * are reachable from a mobile device key.
 *
 * Room (via [dao]) is the UI's source of truth for the list — [observeBots]
 * reads from it, [refresh] is what actually talks to the network and
 * writes the result back in. This is deliberately a simple "cache + full
 * replace" shape, not a NetworkBoundResource: there's exactly one list
 * endpoint, no real pagination, and no offline write path (every mutation
 * below is a server-authoritative action), so a heavier abstraction
 * wouldn't earn its keep here. Bot tokens are never written to [dao] — see
 * BotEntity's doc — so editing an existing bot always re-fetches fresh via
 * [getForEdit] instead of reading a cached credential. */
@Singleton
class BotsRepository @Inject constructor(
    private val apiService: ApiService,
    private val dao: BotDao,
    private val json: Json,
) {
    fun observeBots(): Flow<List<BotInstance>> = dao.observeAll().map { entities -> entities.map(::toDomain) }

    suspend fun refresh(): Result<Unit> = runCatching {
        val remote = apiService.bots()
        dao.replaceAll(remote.map(::toEntity))
    }

    /** Always network, never Room — the one place this app reads a bot's
     * real credentials back out, right before showing them in the (already
     * biometric-gated) edit form. */
    suspend fun getForEdit(id: Int): BotInstance = apiService.bot(id)

    suspend fun personas(): List<PersonaPreset> = apiService.personas()

    suspend fun create(
        name: String,
        platform: String,
        backend: String,
        model: String?,
        persona: String,
        credentials: BotCredentials,
        allowedUserIds: List<String>,
    ) {
        apiService.createBot(
            BotWriteRequest(
                name = name,
                platform = platform,
                backend = backend,
                model = model?.takeIf { it.isNotBlank() },
                persona = persona,
                credentials = credentials,
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
        credentials: BotCredentials,
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
                credentials = credentials,
                allowedUserIds = allowedUserIds,
                enabled = enabled,
            ),
        )
    }

    suspend fun delete(id: Int) {
        apiService.deleteBot(id)
        dao.deleteById(id)
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

    private fun toEntity(bot: BotInstance): BotEntity = BotEntity(
        id = bot.id,
        name = bot.name,
        platform = bot.platform,
        backend = bot.backend,
        enabled = bot.enabled,
        model = bot.model,
        persona = bot.persona,
        allowedUserIdsJson = json.encodeToString(bot.allowedUserIds),
        adminUserIdsJson = json.encodeToString(bot.adminUserIds),
        canTargetJson = json.encodeToString(bot.canTarget),
        lastError = bot.lastError,
        liveRunning = bot.liveRunning,
    )

    private fun toDomain(entity: BotEntity): BotInstance = BotInstance(
        id = entity.id,
        name = entity.name,
        platform = entity.platform,
        backend = entity.backend,
        enabled = entity.enabled,
        model = entity.model,
        persona = entity.persona,
        credentials = BotCredentials(), // never cached — see BotEntity's doc
        allowedUserIdsRaw = JsonArray(json.decodeFromString<List<String>>(entity.allowedUserIdsJson).map { JsonPrimitive(it) }),
        adminUserIdsRaw = JsonArray(json.decodeFromString<List<String>>(entity.adminUserIdsJson).map { JsonPrimitive(it) }),
        canTarget = json.decodeFromString(entity.canTargetJson),
        lastError = entity.lastError,
        liveRunning = entity.liveRunning,
    )
}

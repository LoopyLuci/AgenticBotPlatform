package com.botserver.mobile.data.db

import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * The offline cache of GET /api/bots — deliberately excludes
 * [com.botserver.mobile.data.dto.BotCredentials] (the real platform bot
 * token). Room's SQLite file isn't encrypted the way CredentialStore's
 * EncryptedSharedPreferences is, so persisting a bot token here would
 * write it to disk in the clear — a real regression against this app's
 * other hardening. Editing an existing bot instead fetches its token
 * fresh over the network at that moment (see BotsRepository.getForEdit),
 * gated behind the same biometric check as opening the edit form.
 *
 * List fields that aren't sorted/filtered on (allowed/admin user ids,
 * can-target) are kept as JSON text rather than normalized into their own
 * tables — these are small, opaque-to-SQL blobs the app never queries by
 * content, just round-trips.
 */
@Entity(tableName = "bots")
data class BotEntity(
    @PrimaryKey val id: Int,
    val name: String,
    val platform: String,
    val backend: String,
    val enabled: Boolean,
    val model: String?,
    val persona: String,
    val allowedUserIdsJson: String,
    val adminUserIdsJson: String,
    val canTargetJson: String,
    val lastError: String?,
    val liveRunning: Boolean,
)

package com.botserver.mobile.data

import android.content.Context
import com.botserver.mobile.nlu.ModelFile
import dagger.hilt.android.qualifiers.ApplicationContext
import java.io.File
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/** Loads the Support Bot's Kotlin classifier model — a bundled asset
 * (`assets/support_bot_model.json`, the same file
 * bot/support_bot/hybrid.py's export_current_model() produced at build
 * time) as the always-available baseline, replaceable with a freshly
 * fetched copy from the server whenever the app has a live connection.
 * Local classification is advisory-only (see
 * com.botserver.mobile.nlu.HybridClassifier's own docstring) — the
 * server always independently re-validates and gates every real
 * action, so a stale bundled model is a UX quality issue, never a
 * safety one. */
@Singleton
class ModelRepository @Inject constructor(
    private val apiService: ApiService,
    @ApplicationContext private val context: Context,
) {
    private val json = Json { ignoreUnknownKeys = true }
    private val cachedModelFile = File(context.filesDir, "support_bot_model.json")

    @Volatile private var cached: ModelFile? = null

    /** The current model — never null: a previously-fetched cache on
     * disk wins if present, otherwise the bundled asset shipped with
     * the app. */
    fun current(): ModelFile {
        cached?.let { return it }
        val fromDisk = cachedModelFile.takeIf { it.exists() }
            ?.let { runCatching { json.decodeFromString<ModelFile>(it.readText()) }.getOrNull() }
        val model = fromDisk ?: loadBundledAsset()
        cached = model
        return model
    }

    private fun loadBundledAsset(): ModelFile {
        val text = context.assets.open("support_bot_model.json").bufferedReader().use { it.readText() }
        return json.decodeFromString(text)
    }

    /** Fetches the server's current model and caches it to disk only if
     * it actually differs (by training_data_hash) — called
     * opportunistically whenever the app has a live connection, never
     * required for local classification to keep working. Returns true
     * if a newer model was fetched and is now in use. */
    suspend fun refreshFromServer(): Boolean {
        val fetched = apiService.supportBotModel()
        if (fetched.trainingDataHash == current().trainingDataHash) return false
        cachedModelFile.writeText(json.encodeToString(fetched))
        cached = fetched
        return true
    }
}

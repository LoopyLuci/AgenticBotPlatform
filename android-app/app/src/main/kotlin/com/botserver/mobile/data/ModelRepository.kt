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

/** Loads the Support Bot's Kotlin classifier models — ONE PER Knowledge
 * Module (next-generation modular hybrid plan, Phase 4; previously a
 * single global model). Bundled assets
 * (`assets/support_bot_models/<moduleId>.json`, one per module, plus
 * `assets/support_bot_manifest.json` listing which modules exist — all
 * generated the same way bot/support_bot/hybrid.py's
 * export_current_model() always has, just once per module now) are the
 * always-available baseline, replaceable with freshly fetched copies
 * from the server whenever the app has a live connection. Local
 * classification is advisory-only (see
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
    private val cacheDir = File(context.filesDir, "support_bot_models")

    @Volatile private var cached: Map<String, ModelFile>? = null

    /** Every enabled module's current model — never empty: a
     * previously-fetched cache on disk wins per module if present,
     * otherwise that module's bundled asset. A module present in the
     * bundled assets but missing from a partial disk cache (e.g. the
     * app was updated with a new module after the last refresh) falls
     * back to its own bundled copy individually, not the whole set. */
    fun current(): Map<String, ModelFile> {
        cached?.let { return it }
        val moduleIds = bundledModuleIds()
        val models = moduleIds.associateWith { moduleId -> loadOne(moduleId) }
        cached = models
        return models
    }

    private fun loadOne(moduleId: String): ModelFile {
        val cacheFile = File(cacheDir, "$moduleId.json")
        val fromDisk = cacheFile.takeIf { it.exists() }
            ?.let { runCatching { json.decodeFromString<ModelFile>(it.readText()) }.getOrNull() }
        return fromDisk ?: loadBundledAsset(moduleId)
    }

    private fun bundledModuleIds(): List<String> =
        context.assets.list("support_bot_models")?.mapNotNull { name ->
            name.takeIf { it.endsWith(".json") }?.removeSuffix(".json")
        }.orEmpty()

    private fun loadBundledAsset(moduleId: String): ModelFile {
        val text = context.assets.open("support_bot_models/$moduleId.json").bufferedReader().use { it.readText() }
        return json.decodeFromString(text)
    }

    /** Fetches the server's current manifest, then every enabled
     * module's model, caching to disk (per module) only when it
     * actually differs (by training_data_hash) from what's already
     * cached/bundled — called opportunistically whenever the app has a
     * live connection, never required for local classification to keep
     * working. Returns true if at least one module's model was actually
     * updated. A single module's fetch failing doesn't abort the rest —
     * best-effort per module, same as the rest of this repository's
     * "never let a network hiccup break offline classification"
     * contract. */
    suspend fun refreshFromServer(): Boolean {
        val manifest = runCatching { apiService.supportBotManifest() }.getOrNull() ?: return false
        val existing = current()
        var anyUpdated = false
        val updated = existing.toMutableMap()

        for (moduleId in manifest.keys) {
            if (manifest[moduleId]?.enabled != true) continue
            val fetched = runCatching { apiService.supportBotModel(moduleId) }.getOrNull() ?: continue
            if (fetched.trainingDataHash == existing[moduleId]?.trainingDataHash) continue
            cacheDir.mkdirs()
            File(cacheDir, "$moduleId.json").writeText(json.encodeToString(fetched))
            updated[moduleId] = fetched
            anyUpdated = true
        }

        if (anyUpdated) cached = updated
        return anyUpdated
    }
}

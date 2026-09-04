package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Mirrors one row from GET /api/providers — see bot/providers.py's
 * config/providers.yaml-backed registry. Never carries a real inline API
 * key's value back — only whether one is set — same "no extra redaction
 * on read, but never echo a secret back either" stance the desktop
 * dashboard's own providers card follows. */
@Serializable
data class ProviderInfo(
    val name: String,
    @SerialName("base_url") val baseUrl: String? = null,
    val protocol: String = "openai",
    @SerialName("api_key_env") val apiKeyEnv: String? = null,
    @SerialName("has_inline_key") val hasInlineKey: Boolean = false,
    @SerialName("catalog_id") val catalogId: String? = null,
)

@Serializable
data class ProvidersListResponse(val providers: List<ProviderInfo> = emptyList())

/** One models.dev catalog entry with a real OpenAI-compatible base URL —
 * see bot/model_pricing.py's list_known_providers(). Powers the
 * catalog-assisted "add provider" picker: selecting one prefills a real
 * base_url and a suggested API-key env var name. */
@Serializable
data class CatalogProvider(
    val id: String,
    val name: String,
    val api: String,
    val env: List<String> = emptyList(),
)

@Serializable
data class ProvidersCatalogResponse(val providers: List<CatalogProvider> = emptyList())

/** One row from GET /api/providers/{name}/models — see
 * bot/models.py's browse_provider_models(). `free` is null for a model
 * models.dev has no cost figures for (shown as "unpriced", not assumed
 * paid or free); `enabled` reflects the model's EFFECTIVE toggle state —
 * an explicit override if one exists, else the free-default-on/
 * paid-default-off rule bot.models._resolve_effective_enabled() applies. */
@Serializable
data class BrowseModel(
    val id: String,
    val free: Boolean? = null,
    val input: Double? = null,
    val output: Double? = null,
    val enabled: Boolean = true,
)

@Serializable
data class ProviderModelsResponse(val models: List<BrowseModel> = emptyList())

/** Body for POST /api/providers — creating or updating a named provider.
 * Mirrors bot.providers.set_provider()'s keyword args. */
@Serializable
data class SetProviderRequest(
    val name: String,
    @SerialName("base_url") val baseUrl: String,
    val protocol: String = "openai",
    @SerialName("api_key_env") val apiKeyEnv: String? = null,
    @SerialName("api_key") val apiKey: String? = null,
    @SerialName("catalog_id") val catalogId: String? = null,
)

@Serializable
data class ModelToggleRequest(@SerialName("model_id") val modelId: String, val enabled: Boolean)

@Serializable
data class ModelTogglePaidRequest(val enabled: Boolean)

@Serializable
data class ModelTogglePaidResponse(val ok: Boolean = false, val count: Int = 0)

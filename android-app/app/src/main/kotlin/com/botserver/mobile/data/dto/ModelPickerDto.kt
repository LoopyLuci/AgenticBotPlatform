package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** One entry in the top-level provider list — mirrors
 * bot.commands.instance_model_page()'s "providers" mode payload, the
 * same data Telegram's interactive /model command renders as an inline
 * keyboard (see bot/handlers.py's _model_providers_page). */
@Serializable
data class ModelPickerProvider(
    val idx: Int,
    val name: String,
    val count: Int,
    @SerialName("free_count") val freeCount: Int,
    @SerialName("is_current") val isCurrent: Boolean,
)

/** GET /api/bots/{id}/model-picker's response — a two-level picker:
 * mode == "providers" for a backend with more than one model provider
 * (only Hermes backends), mode == "models" for the paginated model list
 * within one provider (or straight away, for a single-provider backend
 * like the Claude api family). Both modes share one response shape
 * (matching the Python dict this mirrors exactly) rather than two
 * separate DTOs, since JSON has no polymorphic discriminator here. */
@Serializable
data class ModelPickerResponse(
    val mode: String,
    val backend: String,
    val providers: List<ModelPickerProvider>? = null,
    val provider: String? = null,
    @SerialName("provider_idx") val providerIdx: Int? = null,
    @SerialName("multi_provider") val multiProvider: Boolean = false,
    val page: Int = 0,
    @SerialName("total_pages") val totalPages: Int = 1,
    val models: List<String>? = null,
    @SerialName("current_model") val currentModel: String? = null,
    @SerialName("has_known_list") val hasKnownList: Boolean = true,
)

@Serializable
data class SetModelRequest(val model: String)

@Serializable
data class SetModelResponse(val ok: Boolean, val message: String)

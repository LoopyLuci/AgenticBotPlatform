package com.botserver.mobile.nlu

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/** Mirrors the exact JSON shape bot/support_bot/model_io.py saves and
 * bot/support_bot/hybrid.py's export_current_model() serves at
 * GET /api/support-bot/model. `eval` is opaque here (dashboard-only
 * data, not needed for classification) so it's kept as raw JSON rather
 * than a fully-typed shape that would need updating every time the
 * Python side's eval block grows a new field. */
@Serializable
data class ModelFile(
    @SerialName("format_version") val formatVersion: Int,
    @SerialName("training_data_hash") val trainingDataHash: String,
    val intents: List<String>,
    val tfidf: TfidfState,
    val nn: NnState,
    val eval: Map<String, JsonElement> = emptyMap(),
)

/** Mirrors bot/support_bot/model.py's TfidfCentroidModel.export_state(). */
@Serializable
data class TfidfState(
    val idf: Map<String, Double>,
    val centroids: Map<String, Map<String, Double>>,
    @SerialName("confidence_threshold") val confidenceThreshold: Double,
)

/** Mirrors bot/support_bot/nn_model.py's NeuralIntentClassifier.export_state(). */
@Serializable
data class NnState(
    val vocab: Map<String, Int>,
    val idf: List<Double>,
    val classes: List<String>,
    @SerialName("hidden_units") val hiddenUnits: Int,
    val w1: List<List<Double>>,
    val b1: List<Double>,
    val w2: List<List<Double>>,
    val b2: List<Double>,
    @SerialName("confidence_threshold") val confidenceThreshold: Double,
)

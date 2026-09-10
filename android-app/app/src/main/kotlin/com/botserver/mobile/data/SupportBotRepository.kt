package com.botserver.mobile.data

import com.botserver.mobile.data.dto.SupportBotAskRequest
import com.botserver.mobile.data.dto.SupportBotClassifyRequest
import com.botserver.mobile.data.dto.SupportBotConfirmRequest
import com.botserver.mobile.data.dto.SupportBotReply
import com.botserver.mobile.nlu.CascadeClassifier
import com.botserver.mobile.nlu.CascadeResult
import com.botserver.mobile.nlu.HybridClassifier
import javax.inject.Inject
import javax.inject.Singleton

/** Thin wrapper over the desktop server's local Support Bot — same
 * bot/support_bot/ classifier and management actions the desktop
 * dashboard's Support Bot panel talks to, just from the phone.
 *
 * [classifyLocally] runs Tier 0 of the Support Bot NLU cascade (one
 * HybridClassifier per enabled Knowledge Module — see
 * com.botserver.mobile.nlu.CascadeClassifier), sent along as an
 * optional fast-path hint in [ask]; when Tier 0 itself is unsure,
 * [classifyLocallyOrEscalate] calls Tier 1 (the server's own
 * always-freshest, unpartitioned model) for a second opinion. Both are
 * purely advisory: the server always independently re-validates and
 * gates every real action regardless of either hint, so local/Tier-1
 * classification only ever decides what the user meant, never executes
 * anything itself. */
@Singleton
class SupportBotRepository @Inject constructor(
    private val apiService: ApiService,
    private val modelRepository: ModelRepository,
) {
    /** Instant, fully offline classification — for showing a preview
     * ("did you mean: restart bot X?") before the network round-trip
     * completes, or when there's no connection at all. Never used to
     * execute anything by itself. */
    fun classifyLocally(text: String): CascadeResult {
        val models = modelRepository.current()
        val classifiers = models.mapValues { (_, model) -> HybridClassifier(model) }
        return CascadeClassifier(classifiers).classify(text, models.keys)
    }

    /** Tier 0, escalating to Tier 1 (a live call to the server's own
     * always-freshest, unpartitioned model) only when Tier 0 itself
     * came back "unknown" — keeping this off the hot path for every
     * message Tier 0 already resolves confidently. Falls back to Tier
     * 0's own "unknown" if Tier 1 isn't reachable (no connection). */
    suspend fun classifyLocallyOrEscalate(text: String): CascadeResult {
        val tier0 = runCatching { classifyLocally(text) }.getOrNull()
            ?: CascadeResult("unknown", 0.0, null, "unknown", emptyMap())
        if (tier0.intent != "unknown") return tier0

        val tier1 = runCatching { apiService.supportBotClassify(SupportBotClassifyRequest(text)) }.getOrNull()
            ?: return tier0
        return CascadeResult(tier1.intent, tier1.confidence, null, tier1.source, tier0.perModule)
    }

    suspend fun ask(text: String): SupportBotReply {
        // Best-effort — a local-classification failure (e.g. a corrupt
        // cached model file) must never block the real request.
        val localIntent = runCatching { classifyLocally(text).intent }.getOrNull()?.takeIf { it != "unknown" }
        return apiService.supportBotAsk(SupportBotAskRequest(text, clientIntent = localIntent))
    }

    suspend fun confirm(token: String): SupportBotReply = apiService.supportBotConfirm(SupportBotConfirmRequest(token))
}

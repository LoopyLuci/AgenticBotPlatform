package com.botserver.mobile.data

import com.botserver.mobile.data.dto.SupportBotAskRequest
import com.botserver.mobile.data.dto.SupportBotConfirmRequest
import com.botserver.mobile.data.dto.SupportBotReply
import com.botserver.mobile.nlu.HybridClassifier
import com.botserver.mobile.nlu.HybridResult
import javax.inject.Inject
import javax.inject.Singleton

/** Thin wrapper over the desktop server's local Support Bot — same
 * bot/support_bot/ classifier and management actions the desktop
 * dashboard's Support Bot panel talks to, just from the phone. As of
 * Phase 6 of the Support Bot NLU upgrade plan, [ask] also runs a local,
 * on-device classification (com.botserver.mobile.nlu) and sends its
 * guess along as an optional fast-path hint — purely advisory: the
 * server always independently re-validates and gates every real
 * action, so local classification only ever decides what the user
 * meant, never executes anything itself. */
@Singleton
class SupportBotRepository @Inject constructor(
    private val apiService: ApiService,
    private val modelRepository: ModelRepository,
) {
    /** Instant, fully offline classification — for showing a preview
     * ("did you mean: restart bot X?") before the network round-trip
     * completes, or when there's no connection at all. Never used to
     * execute anything by itself. */
    fun classifyLocally(text: String): HybridResult = HybridClassifier(modelRepository.current()).classify(text)

    suspend fun ask(text: String): SupportBotReply {
        // Best-effort — a local-classification failure (e.g. a corrupt
        // cached model file) must never block the real request.
        val localIntent = runCatching { classifyLocally(text).intent }.getOrNull()?.takeIf { it != "unknown" }
        return apiService.supportBotAsk(SupportBotAskRequest(text, clientIntent = localIntent))
    }

    suspend fun confirm(token: String): SupportBotReply = apiService.supportBotConfirm(SupportBotConfirmRequest(token))
}

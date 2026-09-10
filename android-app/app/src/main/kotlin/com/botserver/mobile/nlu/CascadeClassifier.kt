package com.botserver.mobile.nlu

/** Exact port of bot/support_bot/cascade.py's cross-module reduction —
 * Tier 0 of the Support Bot NLU cascade. One HybridClassifier per
 * enabled Knowledge Module runs completely independently (each is just
 * today's HybridClassifier, unmodified, over that module's own
 * ModelFile); this class's job is only to pick the single best answer
 * across all of them. */
data class CascadeResult(
    val intent: String,
    val confidence: Double,
    val moduleId: String?,
    val source: String, // "ensemble" | "tfidf" | "nn" | "unknown"
    val perModule: Map<String, Triple<String, Double, String>>,
)

class CascadeClassifier(private val moduleClassifiers: Map<String, HybridClassifier>) {
    fun classify(text: String, enabledModuleIds: Collection<String>): CascadeResult {
        val perModule = mutableMapOf<String, Triple<String, Double, String>>()
        for (moduleId in enabledModuleIds) {
            val classifier = moduleClassifiers[moduleId] ?: continue
            val result = classifier.classify(text)
            perModule[moduleId] = Triple(result.intent, result.confidence, result.source)
        }
        val (intent, confidence, source, moduleId) = reduceCrossModule(perModule)
        return CascadeResult(intent, confidence, moduleId, source, perModule)
    }

    companion object {
        /** The pure reduction itself — mirrors
         * bot/support_bot/cascade.py's reduce_cross_module() exactly,
         * including its "ties favor tfidf" rule. Extracted as a pure
         * function (same reasoning as HybridClassifier.vote()) so it's
         * directly testable against fixed inputs, no real trained
         * classifier or model file needed on either platform. Returns
         * (intent, confidence, source, moduleId) — moduleId is null only
         * when every module said "unknown". */
        fun reduceCrossModule(perModule: Map<String, Triple<String, Double, String>>): CascadeReduction {
            var best: CascadeReduction? = null
            for ((moduleId, vote) in perModule) {
                val (intent, confidence, source) = vote
                if (intent == "unknown") continue
                val isBetter = best == null ||
                    confidence > best.confidence ||
                    (confidence == best.confidence && source == "tfidf" && best.source != "tfidf")
                if (isBetter) {
                    best = CascadeReduction(intent, confidence, source, moduleId)
                }
            }
            return best ?: CascadeReduction("unknown", 0.0, "unknown", null)
        }
    }
}

data class CascadeReduction(val intent: String, val confidence: Double, val source: String, val moduleId: String?)

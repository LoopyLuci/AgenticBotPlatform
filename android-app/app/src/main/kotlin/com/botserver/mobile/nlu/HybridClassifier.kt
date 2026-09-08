package com.botserver.mobile.nlu

/** Mirrors bot/support_bot/hybrid.py's HybridResult — the fields a
 * caller needs to decide what to show/send, not just the final guess. */
data class HybridResult(
    val intent: String,
    val confidence: Double,
    val tfidfIntent: String,
    val tfidfConfidence: Double,
    val nnIntent: String,
    val nnConfidence: Double,
    val agreed: Boolean,
    val source: String, // "ensemble" | "tfidf" | "nn" | "unknown"
)

/** Exact port of bot/support_bot/hybrid.py's `classify()`/`vote()` —
 * runs BOTH sub-classifiers and combines them with the identical
 * decision rule the Python original uses, so a phone offline gets the
 * same answer the desktop server would have given it. See this
 * class's own module for why hybrid, not "pick one": the two
 * sub-models fail differently, and agreement between them is the
 * strongest confidence signal. */
class HybridClassifier(model: ModelFile) {
    private val centroid = CentroidClassifier(model.tfidf)
    private val mlp = MlpClassifier(model.nn)

    fun classify(text: String): HybridResult {
        val (tfidfIntent, tfidfConfidence) = centroid.predict(text)
        val (nnIntent, nnConfidence) = mlp.predict(text)
        val (intent, confidence, source, agreed) = vote(tfidfIntent, tfidfConfidence, nnIntent, nnConfidence)
        return HybridResult(
            intent = intent, confidence = confidence,
            tfidfIntent = tfidfIntent, tfidfConfidence = tfidfConfidence,
            nnIntent = nnIntent, nnConfidence = nnConfidence,
            agreed = agreed, source = source,
        )
    }

    companion object {
        /** The hybrid decision rule itself, exposed as a pure function
         * (mirrors bot/support_bot/hybrid.py's own `vote()`) so it can
         * be tested directly against fixed inputs, independent of
         * either sub-model's real math. Returns
         * (intent, confidence, source, agreed). */
        fun vote(tfidfIntent: String, tfidfConfidence: Double, nnIntent: String, nnConfidence: Double): VoteResult {
            val agreed = tfidfIntent == nnIntent && tfidfIntent != "unknown"
            val (intent, confidence, source) = when {
                agreed -> Triple(tfidfIntent, maxOf(tfidfConfidence, nnConfidence), "ensemble")
                tfidfIntent != "unknown" && nnIntent != "unknown" ->
                    if (tfidfConfidence >= nnConfidence) Triple(tfidfIntent, tfidfConfidence, "tfidf")
                    else Triple(nnIntent, nnConfidence, "nn")
                tfidfIntent != "unknown" -> Triple(tfidfIntent, tfidfConfidence, "tfidf")
                nnIntent != "unknown" -> Triple(nnIntent, nnConfidence, "nn")
                else -> Triple("unknown", maxOf(tfidfConfidence, nnConfidence), "unknown")
            }
            return VoteResult(intent, confidence, source, agreed)
        }
    }
}

data class VoteResult(val intent: String, val confidence: Double, val source: String, val agreed: Boolean)

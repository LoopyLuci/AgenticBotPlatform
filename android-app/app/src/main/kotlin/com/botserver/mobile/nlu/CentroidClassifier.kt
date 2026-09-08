package com.botserver.mobile.nlu

import kotlin.math.sqrt

/** Exact port of bot/support_bot/model.py's TfidfCentroidModel — TF-IDF
 * vector vs. per-intent centroid, nearest by cosine similarity. Must
 * classify identically to the Python original given the same
 * TfidfState (see ModelFile.kt); see Phase 7's golden-fixture tests for
 * the cross-platform proof. */
class CentroidClassifier(private val state: TfidfState) {
    private fun vectorize(tokens: List<String>): Map<String, Double> {
        if (tokens.isEmpty()) return emptyMap()
        val counts = tokens.groupingBy { it }.eachCount()
        val total = counts.values.sum().takeIf { it > 0 } ?: 1
        return counts.mapValues { (term, count) -> (count.toDouble() / total) * (state.idf[term] ?: 1.0) }
    }

    private fun dot(a: Map<String, Double>, b: Map<String, Double>): Double =
        a.entries.sumOf { (term, value) -> value * (b[term] ?: 0.0) }

    private fun norm(a: Map<String, Double>): Double {
        val n = sqrt(a.values.sumOf { it * it })
        return if (n == 0.0) 1.0 else n
    }

    private fun cosine(a: Map<String, Double>, b: Map<String, Double>): Double = dot(a, b) / (norm(a) * norm(b))

    /** Returns (intent, confidence); intent is "unknown" when nothing
     * clears the model's own confidence_threshold — same contract as
     * the Python original's predict(). */
    fun predict(text: String): Pair<String, Double> {
        val vec = vectorize(Tokenizer.unigrams(text))
        if (vec.isEmpty()) return "unknown" to 0.0

        var bestIntent: String? = null
        var bestScore = -1.0
        for ((intent, centroid) in state.centroids) {
            val score = cosine(vec, centroid)
            if (score > bestScore) {
                bestIntent = intent
                bestScore = score
            }
        }
        if (bestIntent == null || bestScore < state.confidenceThreshold) {
            return "unknown" to maxOf(bestScore, 0.0)
        }
        return bestIntent to bestScore
    }
}

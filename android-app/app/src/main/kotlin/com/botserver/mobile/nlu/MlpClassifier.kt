package com.botserver.mobile.nlu

import kotlin.math.exp

/** Exact port of bot/support_bot/nn_model.py's NeuralIntentClassifier —
 * TF-IDF (unigram+adjacent-bigram) features through a one-hidden-layer
 * ReLU network with softmax output. Single-input forward pass only
 * (Android never needs to batch), but the arithmetic is identical to
 * numpy's `X @ w1`/`X @ w2` for a batch of one row. See Phase 7's
 * golden-fixture tests for the cross-platform proof. */
class MlpClassifier(private val state: NnState) {
    private fun vectorize(tokens: List<String>): DoubleArray {
        val x = DoubleArray(state.vocab.size)
        if (tokens.isEmpty()) return x
        val counts = tokens.groupingBy { it }.eachCount()
        val total = counts.values.sum().takeIf { it > 0 } ?: 1
        for ((term, count) in counts) {
            val j = state.vocab[term] ?: continue
            x[j] = (count.toDouble() / total) * state.idf[j]
        }
        return x
    }

    /** Returns (intent, confidence); intent is "unknown" when nothing
     * clears the model's own confidence_threshold — same contract as
     * the Python original's predict(). */
    fun predict(text: String): Pair<String, Double> {
        if (state.classes.isEmpty()) return "unknown" to 0.0

        val x = vectorize(Tokenizer.unigramsAndBigrams(text))

        // z1 = x . w1 + b1 ; a1 = relu(z1)
        val a1 = DoubleArray(state.hiddenUnits)
        for (h in 0 until state.hiddenUnits) {
            var sum = state.b1[h]
            for (f in x.indices) sum += x[f] * state.w1[f][h]
            a1[h] = maxOf(sum, 0.0)
        }

        // z2 = a1 . w2 + b2 ; softmax(z2)
        val nClasses = state.classes.size
        val z2 = DoubleArray(nClasses)
        for (c in 0 until nClasses) {
            var sum = state.b2[c]
            for (h in a1.indices) sum += a1[h] * state.w2[h][c]
            z2[c] = sum
        }
        val maxZ2 = z2.max()
        val expZ2 = DoubleArray(nClasses) { exp(z2[it] - maxZ2) }
        val sumExp = expZ2.sum()
        val probs = DoubleArray(nClasses) { expZ2[it] / sumExp }

        var bestIdx = 0
        for (i in 1 until nClasses) if (probs[i] > probs[bestIdx]) bestIdx = i
        val confidence = probs[bestIdx]
        val intent = state.classes[bestIdx]

        if (confidence < state.confidenceThreshold) return "unknown" to confidence
        return intent to confidence
    }
}

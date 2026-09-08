package com.botserver.mobile.nlu

import org.junit.Assert.assertEquals
import org.junit.Test

class CentroidClassifierTest {
    private fun state(threshold: Double = 0.1) = TfidfState(
        idf = mapOf("restart" to 1.0, "bot" to 1.0, "status" to 1.0, "healthy" to 1.0),
        centroids = mapOf(
            "bot_restart" to mapOf("restart" to 1.0, "bot" to 0.5),
            "status" to mapOf("status" to 1.0, "healthy" to 0.5),
        ),
        confidenceThreshold = threshold,
    )

    @Test
    fun `picks the nearest centroid by cosine similarity`() {
        val classifier = CentroidClassifier(state())
        val (intent, confidence) = classifier.predict("restart the bot")
        assertEquals("bot_restart", intent)
        assert(confidence > 0.0)
    }

    @Test
    fun `returns unknown for text with no vocabulary overlap`() {
        val classifier = CentroidClassifier(state())
        val (intent, _) = classifier.predict("completely unrelated words here")
        assertEquals("unknown", intent)
    }

    @Test
    fun `returns unknown when the best score is below the threshold`() {
        val classifier = CentroidClassifier(state(threshold = 0.99))
        val (intent, _) = classifier.predict("restart the bot")
        assertEquals("unknown", intent)
    }

    @Test
    fun `an unknown term not in idf still contributes with a default weight`() {
        // "please" isn't in idf at all — model.py's _vectorize uses
        // idf.get(term, 1.0), so it must not crash and must still let
        // the known terms drive the match.
        val classifier = CentroidClassifier(state())
        val (intent, _) = classifier.predict("please restart the bot")
        assertEquals("bot_restart", intent)
    }
}

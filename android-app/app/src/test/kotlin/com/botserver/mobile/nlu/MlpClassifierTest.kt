package com.botserver.mobile.nlu

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** A hand-computable 2-feature/1-hidden-unit/2-class network — small
 * enough to verify the exact forward-pass arithmetic (relu + softmax)
 * matches bot/support_bot/nn_model.py's numpy implementation, not just
 * "picks something reasonable." */
class MlpClassifierTest {
    private fun state(threshold: Double = 0.5) = NnState(
        vocab = mapOf("restart" to 0, "bot" to 1),
        idf = listOf(1.0, 1.0),
        classes = listOf("a", "b"),
        hiddenUnits = 1,
        w1 = listOf(listOf(1.0), listOf(1.0)), // [feature][hidden]
        b1 = listOf(0.0),
        w2 = listOf(listOf(1.0, 0.0)), // [hidden][class]
        b2 = listOf(0.0, 0.0),
        confidenceThreshold = threshold,
    )

    @Test
    fun `forward pass picks class a with the expected confidence`() {
        val classifier = MlpClassifier(state(threshold = 0.5))
        val (intent, confidence) = classifier.predict("restart bot")
        assertEquals("a", intent)
        // Hand-computed: x=[1/3,1/3], z1=0.667, a1=0.667, z2=[0.667,0],
        // softmax ~= [0.661, 0.339].
        assertTrue("expected confidence near 0.66, got $confidence", confidence in 0.64..0.68)
    }

    @Test
    fun `returns unknown when confidence is below threshold`() {
        val classifier = MlpClassifier(state(threshold = 0.99))
        val (intent, _) = classifier.predict("restart bot")
        assertEquals("unknown", intent)
    }

    @Test
    fun `text with no vocabulary overlap yields uniform 50-50 confidence`() {
        val classifier = MlpClassifier(state(threshold = 0.4))
        val (intent, confidence) = classifier.predict("completely unrelated")
        assertEquals("a", intent) // first class wins a tie, matching numpy argmax
        assertTrue(confidence in 0.49..0.51)
    }
}

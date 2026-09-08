package com.botserver.mobile.nlu

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Exact port coverage for bot/support_bot/hybrid.py's `vote()` decision
 * rule — tested directly against fixed inputs, independent of either
 * sub-model's real math (see CentroidClassifierTest/MlpClassifierTest
 * for that). */
class HybridClassifierTest {
    @Test
    fun `both models agreeing on a real intent is an ensemble result`() {
        val result = HybridClassifier.vote("bot_restart", 0.6, "bot_restart", 0.4)
        assertEquals("bot_restart", result.intent)
        assertEquals("ensemble", result.source)
        assertEquals(0.6, result.confidence, 0.0)
        assertTrue(result.agreed)
    }

    @Test
    fun `both confident but disagreeing trusts the higher confidence, ties favor tfidf`() {
        val nnWins = HybridClassifier.vote("a", 0.4, "b", 0.9)
        assertEquals("b", nnWins.intent)
        assertEquals("nn", nnWins.source)
        assertFalse(nnWins.agreed)

        val tie = HybridClassifier.vote("a", 0.5, "b", 0.5)
        assertEquals("a", tie.intent)
        assertEquals("tfidf", tie.source)
    }

    @Test
    fun `only tfidf confident uses tfidf`() {
        val result = HybridClassifier.vote("bot_restart", 0.5, "unknown", 0.1)
        assertEquals("bot_restart", result.intent)
        assertEquals("tfidf", result.source)
    }

    @Test
    fun `only nn confident uses nn`() {
        val result = HybridClassifier.vote("unknown", 0.1, "status", 0.5)
        assertEquals("status", result.intent)
        assertEquals("nn", result.source)
    }

    @Test
    fun `neither confident is unknown`() {
        val result = HybridClassifier.vote("unknown", 0.1, "unknown", 0.2)
        assertEquals("unknown", result.intent)
        assertEquals("unknown", result.source)
        assertFalse(result.agreed)
    }
}

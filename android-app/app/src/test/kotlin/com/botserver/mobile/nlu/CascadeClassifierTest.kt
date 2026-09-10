package com.botserver.mobile.nlu

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** Exact port coverage for bot/support_bot/cascade.py's
 * reduce_cross_module() — tested directly against fixed inputs,
 * independent of any real trained classifier or model file (see
 * GoldenFixturesTest for the full real-model proof, once per-module
 * model export exists). Mirrors tests/test_support_bot_cascade.py's own
 * test_reduce_cross_module_* cases one-to-one. */
class CascadeClassifierTest {
    @Test
    fun `picks highest confidence across modules`() {
        val result = CascadeClassifier.reduceCrossModule(mapOf(
            "bots" to Triple("bot_create", 0.4, "tfidf"),
            "mcp" to Triple("mcp_list", 0.9, "nn"),
        ))
        assertEquals("mcp_list", result.intent)
        assertEquals(0.9, result.confidence, 0.0)
        assertEquals("nn", result.source)
        assertEquals("mcp", result.moduleId)
    }

    @Test
    fun `ties favor tfidf`() {
        val result = CascadeClassifier.reduceCrossModule(mapOf(
            "bots" to Triple("bot_create", 0.5, "nn"),
            "mcp" to Triple("mcp_list", 0.5, "tfidf"),
        ))
        assertEquals("mcp_list", result.intent)
        assertEquals("tfidf", result.source)
        assertEquals("mcp", result.moduleId)
    }

    @Test
    fun `everything unknown returns unknown with no module`() {
        val result = CascadeClassifier.reduceCrossModule(mapOf(
            "bots" to Triple("unknown", 0.1, "unknown"),
            "mcp" to Triple("unknown", 0.05, "unknown"),
        ))
        assertEquals("unknown", result.intent)
        assertEquals(0.0, result.confidence, 0.0)
        assertEquals("unknown", result.source)
        assertNull(result.moduleId)
    }

    @Test
    fun `single confident module wins over unknowns`() {
        val result = CascadeClassifier.reduceCrossModule(mapOf(
            "bots" to Triple("bot_create", 0.6, "ensemble"),
            "mcp" to Triple("unknown", 0.1, "unknown"),
        ))
        assertEquals("bot_create", result.intent)
        assertEquals(0.6, result.confidence, 0.0)
        assertEquals("ensemble", result.source)
        assertEquals("bots", result.moduleId)
    }
}

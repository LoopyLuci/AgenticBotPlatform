package com.botserver.mobile.nlu

import org.junit.Assert.assertEquals
import org.junit.Test

/** Exact-equivalence coverage for bot/support_bot/model.py's `_tokenize`
 * (unigrams) and bot/support_bot/nn_model.py's `_tokens` (unigrams +
 * adjacent bigrams) — same regex (`[a-z0-9]+`), same lowercasing, same
 * bigram join character ("_"). */
class TokenizerTest {
    @Test
    fun `unigrams lowercases and strips punctuation`() {
        assertEquals(listOf("restart", "the", "bot", "please"), Tokenizer.unigrams("Restart the bot, please!"))
    }

    @Test
    fun `unigrams on empty or punctuation only text is empty`() {
        assertEquals(emptyList<String>(), Tokenizer.unigrams("!!! ..."))
    }

    @Test
    fun `unigramsAndBigrams appends adjacent-pair bigrams after the unigrams`() {
        val result = Tokenizer.unigramsAndBigrams("restart the bot")
        assertEquals(listOf("restart", "the", "bot", "restart_the", "the_bot"), result)
    }

    @Test
    fun `unigramsAndBigrams with a single token has no bigrams`() {
        assertEquals(listOf("status"), Tokenizer.unigramsAndBigrams("status"))
    }
}

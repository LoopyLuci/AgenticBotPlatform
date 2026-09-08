package com.botserver.mobile.nlu

/** Exact port of bot/support_bot/model.py's `_TOKEN_RE`/`_tokenize` and
 * bot/support_bot/nn_model.py's `_tokens` — must stay byte-for-byte
 * equivalent (same regex, same lowercasing, same bigram join character)
 * so the two engines classify identically. See
 * bot/support_bot/testdata/golden_fixtures.json (Phase 7) for the
 * cross-platform proof. */
object Tokenizer {
    private val tokenRegex = Regex("[a-z0-9]+")

    /** Unigrams only — mirrors model.py's `_tokenize()`. */
    fun unigrams(text: String): List<String> = tokenRegex.findAll(text.lowercase()).map { it.value }.toList()

    /** Unigrams plus adjacent-pair bigrams ("word1_word2") — mirrors
     * nn_model.py's `_tokens()`. */
    fun unigramsAndBigrams(text: String): List<String> {
        val unigrams = unigrams(text)
        if (unigrams.size < 2) return unigrams
        val bigrams = (0 until unigrams.size - 1).map { i -> "${unigrams[i]}_${unigrams[i + 1]}" }
        return unigrams + bigrams
    }
}

package com.botserver.mobile.data

import com.botserver.mobile.data.dto.SupportBotClassifyRequest
import com.botserver.mobile.data.dto.SupportBotClassifyResponse
import com.botserver.mobile.nlu.ModelFile
import com.botserver.mobile.nlu.NnState
import com.botserver.mobile.nlu.TfidfState
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.every
import io.mockk.mockk
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test

/** SupportBotRepository's classifyLocallyOrEscalate() — the Tier 0 ->
 * Tier 1 escalation decision (next-generation modular hybrid plan,
 * Phase 4). Uses hand-built ModelFile fixtures rather than real trained
 * weights, same convention as HybridClassifierTest/CascadeClassifierTest
 * — this is testing the ESCALATION LOGIC, not the classifier math (see
 * GoldenFixturesTest for that). */
class SupportBotRepositoryTest {
    private val emptyNn = NnState(
        vocab = emptyMap(), idf = emptyList(), classes = emptyList(),
        hiddenUnits = 0, w1 = emptyList(), b1 = emptyList(), w2 = emptyList(), b2 = emptyList(),
        confidenceThreshold = 0.35,
    )

    private fun confidentModelFor(intent: String, term: String): ModelFile = ModelFile(
        formatVersion = 1,
        trainingDataHash = "sha256:test",
        intents = listOf(intent),
        tfidf = TfidfState(
            idf = mapOf(term to 1.0),
            centroids = mapOf(intent to mapOf(term to 1.0)),
            confidenceThreshold = 0.22,
        ),
        nn = emptyNn,
    )

    @Test
    fun `a confident local classification never calls the server`() {
        val modelRepository = mockk<ModelRepository>()
        every { modelRepository.current() } returns mapOf("bots" to confidentModelFor("bot_restart", "restart"))
        val apiService = mockk<ApiService>()
        val repository = SupportBotRepository(apiService, modelRepository)

        val result = runBlocking { repository.classifyLocallyOrEscalate("restart") }

        assertEquals("bot_restart", result.intent)
        coVerify(exactly = 0) { apiService.supportBotClassify(any()) }
    }

    @Test
    fun `an unknown local classification escalates to the server`() {
        val modelRepository = mockk<ModelRepository>()
        every { modelRepository.current() } returns emptyMap()
        val apiService = mockk<ApiService>()
        coEvery { apiService.supportBotClassify(SupportBotClassifyRequest("do a thing")) } returns
            SupportBotClassifyResponse(intent = "bot_restart", confidence = 0.6, source = "tfidf", serverModelVersion = "sha256:server")
        val repository = SupportBotRepository(apiService, modelRepository)

        val result = runBlocking { repository.classifyLocallyOrEscalate("do a thing") }

        assertEquals("bot_restart", result.intent)
        assertEquals(0.6, result.confidence, 0.0)
        coVerify(exactly = 1) { apiService.supportBotClassify(any()) }
    }

    @Test
    fun `an unreachable server falls back to the local unknown result`() {
        val modelRepository = mockk<ModelRepository>()
        every { modelRepository.current() } returns emptyMap()
        val apiService = mockk<ApiService>()
        coEvery { apiService.supportBotClassify(any()) } throws java.io.IOException("no connection")
        val repository = SupportBotRepository(apiService, modelRepository)

        val result = runBlocking { repository.classifyLocallyOrEscalate("do a thing") }

        assertEquals("unknown", result.intent)
    }
}

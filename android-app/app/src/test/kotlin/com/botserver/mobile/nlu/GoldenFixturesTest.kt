package com.botserver.mobile.nlu

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import org.junit.Assert.assertTrue
import org.junit.Test

@Serializable
private data class GoldenFixture(
    val text: String,
    @SerialName("expected_intent") val expectedIntent: String,
    @SerialName("expected_confidence") val expectedConfidence: Double,
)

/** Phase 7 of the "Support Bot NLU upgrade" plan — the hard
 * cross-platform proof. Both `support_bot_model.json` and
 * `golden_fixtures.json` here are copies of
 * bot/support_bot/testdata/golden_fixtures.json and the model file the
 * real Python hybrid classifier was running when those fixtures were
 * generated (see tests/test_support_bot_golden_fixtures.py, the Python
 * side of this same proof). If this test ever fails after a genuine
 * training-data change, regenerate BOTH copies from the real Python
 * classifier — never hand-edit expected values to make it pass. */
class GoldenFixturesTest {
    private val json = Json { ignoreUnknownKeys = true }

    private fun resourceText(name: String): String =
        checkNotNull(javaClass.classLoader?.getResourceAsStream(name)) { "test resource $name not found" }
            .bufferedReader().use { it.readText() }

    @Test
    fun `Kotlin engine reproduces every golden fixture the Python engine produced`() {
        val model = json.decodeFromString(ModelFile.serializer(), resourceText("support_bot_model.json"))
        val fixtures = json.decodeFromString(
            kotlinx.serialization.builtins.ListSerializer(GoldenFixture.serializer()),
            resourceText("golden_fixtures.json"),
        )
        val classifier = HybridClassifier(model)

        val mismatches = fixtures.mapNotNull { fixture ->
            val result = classifier.classify(fixture.text)
            val ok = result.intent == fixture.expectedIntent && Math.abs(result.confidence - fixture.expectedConfidence) < 1e-4
            if (ok) null else "text=${fixture.text} expected=(${fixture.expectedIntent}, ${fixture.expectedConfidence}) got=(${result.intent}, ${result.confidence})"
        }

        assertTrue("golden fixture mismatches (Kotlin engine drifted from Python): $mismatches", mismatches.isEmpty())
    }
}

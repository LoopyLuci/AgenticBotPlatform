package com.botserver.mobile.ui.pairing

import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.botserver.mobile.data.ApiService
import com.botserver.mobile.data.CredentialStore
import com.botserver.mobile.data.PairingRepository
import com.botserver.mobile.data.PushRepository
import com.botserver.mobile.data.dto.ChatRecipientsResponse
import io.mockk.coEvery
import io.mockk.mockk
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** The manual-entry pairing flow — host + key typed in, submitted,
 * verified against a (faked) server, and the screen reports success.
 * ApiService is faked with MockK rather than hit for real: this test is
 * about the screen wiring host/key input through to a working pairing
 * attempt, not about the network layer itself (that's DynamicHostInterceptorTest's
 * job). CredentialStore is real, backed by this test's own instrumentation
 * context — EncryptedSharedPreferences works fine on a real device/emulator. */
@RunWith(AndroidJUnit4::class)
class PairingScreenTest {
    @get:Rule
    val composeRule = createAndroidComposeRule<ComponentActivity>()

    private lateinit var apiService: ApiService
    private lateinit var credentials: CredentialStore

    @Before
    fun setUp() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        apiService = mockk(relaxed = true)
        coEvery { apiService.chatRecipients() } returns ChatRecipientsResponse(instances = emptyList())
        credentials = CredentialStore(context)
        credentials.clear()
    }

    @Test
    fun manualEntryWithAValidKeyPairsSuccessfully() {
        var paired = false
        val repository = PairingRepository(credentials, apiService, PushRepository(apiService, credentials))
        val viewModel = PairingViewModel(repository, credentials)

        composeRule.setContent {
            PairingScreen(viewModel = viewModel, onPaired = { paired = true })
        }

        composeRule.onNodeWithText("Enter key manually instead").performClick()
        composeRule.onNodeWithTag("pairing-host").performTextInput("192.168.1.50:8787")
        composeRule.onNodeWithTag("pairing-key").performTextInput("test-pairing-key")
        composeRule.onNodeWithTag("pairing-submit").performClick()

        composeRule.waitUntil(timeoutMillis = 5_000) { paired }
        assertTrue(credentials.isPaired)
        assertTrue(credentials.apiKey == "test-pairing-key")
    }

    @Test
    fun submittingWithNoKeyShowsAnError() {
        val repository = PairingRepository(credentials, apiService, PushRepository(apiService, credentials))
        val viewModel = PairingViewModel(repository, credentials)

        composeRule.setContent {
            PairingScreen(viewModel = viewModel, onPaired = { })
        }

        composeRule.onNodeWithText("Enter key manually instead").performClick()
        composeRule.onNodeWithTag("pairing-host").performTextInput("192.168.1.50:8787")
        composeRule.onNodeWithTag("pairing-submit").performClick()

        composeRule.onNodeWithText("Paste the key from the dashboard's Mobile tab.").assertExists()
    }
}

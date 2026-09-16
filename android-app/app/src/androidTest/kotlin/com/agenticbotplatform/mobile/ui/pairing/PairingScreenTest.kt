package com.agenticbotplatform.mobile.ui.pairing

import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.test.espresso.Espresso.closeSoftKeyboard
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.agenticbotplatform.mobile.data.ApiService
import com.agenticbotplatform.mobile.data.CredentialStore
import com.agenticbotplatform.mobile.data.GitHubUpdateRepository
import com.agenticbotplatform.mobile.data.PairingRepository
import com.agenticbotplatform.mobile.data.PushRepository
import com.agenticbotplatform.mobile.data.UpdateRepository
import com.agenticbotplatform.mobile.data.dto.ChatRecipientsResponse
import com.agenticbotplatform.mobile.ui.update.AppUpdateViewModel
import io.mockk.coEvery
import io.mockk.mockk
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** The manual-entry pairing flow — a self-contained pairing code (or,
 * via the advanced fallback, host + key typed in separately) submitted
 * and verified against a (faked) server, with the screen reporting
 * success. ApiService is faked with MockK rather than hit for real: this
 * test is about the screen wiring pairing-code/host/key input through to
 * a working pairing attempt, not about the network layer itself (that's
 * DynamicHostInterceptorTest's job). CredentialStore is real, backed by
 * this test's own instrumentation context — EncryptedSharedPreferences
 * works fine on a real device/emulator. GitHubUpdateRepository is faked
 * to never report an update — this test isn't about self-update, and a
 * real GitHubUpdateViewModel touching this device's own SharedPreferences
 * would otherwise be exercised for no reason here.
 *
 * closeSoftKeyboard() runs after every text field is filled and before the
 * submit tap: on a real device the IME shrinks the window, which can push
 * the submit button below the resized viewport and swallow the tap. */
@RunWith(AndroidJUnit4::class)
class PairingScreenTest {
    @get:Rule
    val composeRule = createAndroidComposeRule<ComponentActivity>()

    private lateinit var apiService: ApiService
    private lateinit var credentials: CredentialStore
    private lateinit var updateViewModel: AppUpdateViewModel

    @Before
    fun setUp() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        apiService = mockk(relaxed = true)
        coEvery { apiService.chatRecipients() } returns ChatRecipientsResponse(instances = emptyList())
        credentials = CredentialStore(context)
        credentials.clear()
        val gitHubUpdateRepository = mockk<GitHubUpdateRepository>(relaxed = true)
        coEvery { gitHubUpdateRepository.checkLatest() } returns null
        updateViewModel = AppUpdateViewModel(gitHubUpdateRepository, mockk<UpdateRepository>(relaxed = true))
    }

    @Test
    fun pastingASelfContainedPairingCodePairsSuccessfully() {
        var paired = false
        val repository = PairingRepository(credentials, apiService, PushRepository(apiService, credentials))
        val viewModel = PairingViewModel(repository, credentials)

        composeRule.setContent {
            PairingScreen(viewModel = viewModel, updateViewModel = updateViewModel, onPaired = { paired = true })
        }

        composeRule.onNodeWithText("Enter pairing code manually instead").performClick()
        composeRule.onNodeWithTag("pairing-code").performTextInput("agenticbotplatform://pair?host=192.168.1.50:8787&key=test-pairing-key")
        closeSoftKeyboard()
        composeRule.waitForIdle()
        composeRule.onNodeWithTag("pairing-submit").performClick()

        composeRule.waitUntil(timeoutMillis = 5_000) { paired }
        assertTrue(credentials.isPaired)
        assertTrue(credentials.apiKey == "test-pairing-key")
    }

    @Test
    fun theAdvancedFallbackAcceptsAHostAndKeyEnteredSeparately() {
        var paired = false
        val repository = PairingRepository(credentials, apiService, PushRepository(apiService, credentials))
        val viewModel = PairingViewModel(repository, credentials)

        composeRule.setContent {
            PairingScreen(viewModel = viewModel, updateViewModel = updateViewModel, onPaired = { paired = true })
        }

        composeRule.onNodeWithText("Enter pairing code manually instead").performClick()
        composeRule.onNodeWithText("Paste isn't working? Enter host and key separately").performClick()
        composeRule.onNodeWithTag("pairing-host").performTextInput("192.168.1.50:8787")
        composeRule.onNodeWithTag("pairing-key").performTextInput("test-pairing-key")
        closeSoftKeyboard()
        composeRule.waitForIdle()
        composeRule.onNodeWithTag("pairing-submit").performClick()

        composeRule.waitUntil(timeoutMillis = 5_000) { paired }
        assertTrue(credentials.isPaired)
        assertTrue(credentials.apiKey == "test-pairing-key")
    }

    @Test
    fun submittingWithNoPairingCodeShowsAnError() {
        val repository = PairingRepository(credentials, apiService, PushRepository(apiService, credentials))
        val viewModel = PairingViewModel(repository, credentials)

        composeRule.setContent {
            PairingScreen(viewModel = viewModel, updateViewModel = updateViewModel, onPaired = { })
        }

        composeRule.onNodeWithText("Enter pairing code manually instead").performClick()
        closeSoftKeyboard()
        composeRule.waitForIdle()
        composeRule.onNodeWithTag("pairing-submit").performClick()

        composeRule.onNodeWithText("Paste the pairing code from the dashboard's Mobile tab or a Support Bot reply.").assertExists()
    }
}

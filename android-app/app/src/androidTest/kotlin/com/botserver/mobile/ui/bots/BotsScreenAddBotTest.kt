package com.botserver.mobile.ui.bots

import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.room.Room
import androidx.test.espresso.Espresso.closeSoftKeyboard
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.botserver.mobile.data.ApiService
import com.botserver.mobile.data.BotsRepository
import com.botserver.mobile.data.db.AppDatabase
import com.botserver.mobile.data.dto.BotInstance
import com.botserver.mobile.data.dto.BotWriteRequest
import com.botserver.mobile.data.dto.OkResponse
import com.botserver.mobile.data.dto.PairingListResponse
import com.botserver.mobile.data.dto.PersonaPreset
import io.mockk.coEvery
import io.mockk.mockk
import kotlinx.serialization.json.Json
import org.junit.After
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** The add-a-bot flow: open the form, fill in a name and a token, save,
 * and see it show up in the list. ApiService is faked with MockK — this
 * exercises the screen -> ViewModel -> Repository -> (fake network,
 * real in-memory Room) wiring, not the real backend. Room is a real
 * in-memory database rather than a fake DAO, since Room's own
 * in-memory mode is fast, synchronous-enough for a test, and exercises
 * the actual generated SQL rather than a hand-rolled substitute. */
@RunWith(AndroidJUnit4::class)
class BotsScreenAddBotTest {
    @get:Rule
    val composeRule = createAndroidComposeRule<ComponentActivity>()

    private lateinit var db: AppDatabase
    private lateinit var apiService: ApiService

    @Before
    fun setUp() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        db = Room.inMemoryDatabaseBuilder(context, AppDatabase::class.java).build()
        apiService = mockk(relaxed = true)
        coEvery { apiService.personas() } returns listOf(PersonaPreset("assistant", "Assistant", "💬", "A helpful assistant."))
        coEvery { apiService.pairing(any()) } returns PairingListResponse()
    }

    @After
    fun tearDown() {
        db.close()
    }

    @Test
    fun fillingInNameAndTokenThenSavingAddsTheBotToTheList() {
        val createdBots = mutableListOf<BotInstance>()
        coEvery { apiService.bots() } answers { createdBots.toList() }
        coEvery { apiService.createBot(any()) } coAnswers {
            val request = firstArg<BotWriteRequest>()
            createdBots += BotInstance(
                id = createdBots.size + 1,
                name = request.name,
                platform = request.platform,
                backend = request.backend,
                enabled = true,
            )
            OkResponse(ok = true)
        }
        val repository = BotsRepository(apiService, db.botDao(), Json { ignoreUnknownKeys = true; isLenient = true })
        val viewModel = BotsViewModel(repository)

        composeRule.setContent {
            BotsScreen(viewModel = viewModel)
        }

        composeRule.onNodeWithTag("bots-add-fab").performClick()
        composeRule.onNodeWithTag("bot-form-name").performTextInput("My Test Bot")
        composeRule.onNodeWithTag("bot-form-token").performTextInput("fake-bot-token-12345")
        // Dismiss the IME before tapping save — on a real device the keyboard
        // shrinks the window and can push the save button below the resized
        // viewport, silently swallowing the tap.
        closeSoftKeyboard()
        composeRule.waitForIdle()
        composeRule.onNodeWithTag("bot-form-save").performClick()

        composeRule.waitUntil(timeoutMillis = 5_000) { createdBots.isNotEmpty() }
        composeRule.waitForIdle()
        composeRule.onNodeWithText("My Test Bot").assertExists()
    }
}

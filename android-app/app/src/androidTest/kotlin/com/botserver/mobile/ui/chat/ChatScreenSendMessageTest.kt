package com.botserver.mobile.ui.chat

import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.room.Room
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.botserver.mobile.data.ApiService
import com.botserver.mobile.data.ChatRepository
import com.botserver.mobile.data.LiveEventsClient
import com.botserver.mobile.data.dto.BotInstanceSummary
import com.botserver.mobile.data.dto.ChatRecipientsResponse
import com.botserver.mobile.data.dto.OkResponse
import com.botserver.mobile.data.dto.SendMessageRequest
import com.botserver.mobile.data.db.AppDatabase
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.mockk
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Send-a-message flow: type into the composer, tap send, confirm the
 * request actually reaches ApiService with the typed text. ApiService is
 * faked — this is about the screen -> ViewModel -> Repository wiring, not
 * a real round trip (that path's chunking/pruning/cursor logic already has
 * direct unit-test coverage in ChatRepositorySendFileTest and the Room
 * tests). LiveEventsClient is real but harmlessly unable to connect in a
 * test environment (no real BotServer listening) — its reconnect loop
 * backs off in the background and never blocks this test. */
@RunWith(AndroidJUnit4::class)
class ChatScreenSendMessageTest {
    @get:Rule
    val composeRule = createAndroidComposeRule<ComponentActivity>()

    private lateinit var db: AppDatabase
    private lateinit var apiService: ApiService

    @Before
    fun setUp() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        db = Room.inMemoryDatabaseBuilder(context, AppDatabase::class.java).build()
        apiService = mockk(relaxed = true)
        coEvery { apiService.chatRecipients() } returns ChatRecipientsResponse(
            instances = listOf(BotInstanceSummary(id = 1, name = "Test Bot", platform = "telegram", allowedIds = listOf("12345"), connected = true)),
        )
        coEvery { apiService.chatMessages(any(), any(), any()) } returns emptyList()
        coEvery { apiService.sendMessage(any()) } returns OkResponse(ok = true)
    }

    @After
    fun tearDown() {
        db.close()
    }

    @Test
    fun typingAMessageAndTappingSendReachesTheApiWithThatText() {
        val liveEvents = LiveEventsClient(OkHttpClient(), Json { ignoreUnknownKeys = true; isLenient = true })
        val repository = ChatRepository(apiService, db.chatDao(), liveEvents, InstrumentationRegistry.getInstrumentation().targetContext)
        val viewModel = ChatViewModel(repository)

        composeRule.setContent {
            ChatScreen(viewModel = viewModel)
        }

        // Give the initial recipients fetch (triggered by start()'s first,
        // immediate iteration) a moment to land and pick an active instance.
        composeRule.waitUntil(timeoutMillis = 5_000) {
            viewModel.uiState.value.activeInstanceId != null
        }

        composeRule.onNodeWithTag("chat-message-input").performTextInput("Hello there")
        composeRule.onNodeWithTag("chat-send").performClick()

        composeRule.waitUntil(timeoutMillis = 5_000) {
            // sendMessage() is called asynchronously off the click; poll the
            // fake's recorded call rather than assume it lands the same frame.
            try {
                coVerify(exactly = 1) { apiService.sendMessage(SendMessageRequest(1, "12345", "Hello there")) }
                true
            } catch (e: AssertionError) {
                false
            }
        }
    }
}

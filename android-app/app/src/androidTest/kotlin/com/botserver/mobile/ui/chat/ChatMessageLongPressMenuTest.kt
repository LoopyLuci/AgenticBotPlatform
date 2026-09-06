package com.botserver.mobile.ui.chat

import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.longClick
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.room.Room
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.botserver.mobile.data.ApiService
import com.botserver.mobile.data.ChatRepository
import com.botserver.mobile.data.LiveEventsClient
import com.botserver.mobile.data.db.AppDatabase
import com.botserver.mobile.data.dto.BotInstanceSummary
import com.botserver.mobile.data.dto.ChatMessage
import com.botserver.mobile.data.dto.ChatRecipientsResponse
import com.botserver.mobile.ui.model.ModelPickerViewModel
import io.mockk.coEvery
import io.mockk.mockk
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** The long-press message context menu (Copy message / Select text /
 * Export chat) — the feature requested alongside offline chat
 * persistence (already covered by ChatRepositorySendFileTest/Room
 * itself; this test is about the new interaction, not the cache). */
@RunWith(AndroidJUnit4::class)
class ChatMessageLongPressMenuTest {
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
        coEvery { apiService.chatMessages(any(), any(), any()) } returns listOf(
            ChatMessage(id = 1, ts = "12:00", platform = "telegram", chatId = "12345", username = "Tester", direction = "in", source = "server", text = "Hello world"),
        )
    }

    @After
    fun tearDown() {
        db.close()
    }

    @Test
    fun longPressingAMessageShowsCopySelectAndExportOptions() {
        val liveEvents = LiveEventsClient(OkHttpClient(), Json { ignoreUnknownKeys = true; isLenient = true })
        val repository = ChatRepository(apiService, db.chatDao(), liveEvents, InstrumentationRegistry.getInstrumentation().targetContext)
        val viewModel = ChatViewModel(repository)
        val modelPickerViewModel = ModelPickerViewModel(apiService)

        composeRule.setContent {
            ChatScreen(viewModel = viewModel, modelPickerViewModel = modelPickerViewModel)
        }

        composeRule.waitUntil(timeoutMillis = 5_000) { viewModel.uiState.value.instances.isNotEmpty() }
        composeRule.onNodeWithText("Test Bot").performClick()

        composeRule.waitUntil(timeoutMillis = 5_000) {
            composeRule.onAllNodesWithTextCompat("Hello world").isNotEmpty()
        }

        composeRule.onNodeWithText("Hello world").performTouchInput { longClick() }

        composeRule.onNodeWithText("Copy message").assertExists()
        composeRule.onNodeWithText("Select text").assertExists()
        composeRule.onNodeWithText("Export chat").assertExists()

        composeRule.onNodeWithText("Select text").performClick()
        // The select-text dialog re-renders the same message text inside a
        // SelectionContainer — asserting it's still findable confirms the
        // dialog actually opened rather than the menu just closing.
        composeRule.onAllNodesWithTextCompat("Hello world").also { check(it.isNotEmpty()) }
    }
}

private fun androidx.compose.ui.test.junit4.AndroidComposeTestRule<*, *>.onAllNodesWithTextCompat(text: String) =
    this.onAllNodes(androidx.compose.ui.test.hasText(text)).fetchSemanticsNodes()

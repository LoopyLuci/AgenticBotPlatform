package com.botserver.mobile.ui.chat

import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.ChatRepository
import com.botserver.mobile.data.dto.BotInstanceSummary
import com.botserver.mobile.data.dto.ChatMessage
import dagger.hilt.android.lifecycle.HiltViewModel
import java.io.File
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

/** Per-message attachment-download state — pull-on-demand (see
 * ChatRepository.downloadAttachment): nothing downloads until the user
 * taps the attachment chip. */
sealed interface DownloadState {
    data object Downloading : DownloadState
    data class Ready(val file: File) : DownloadState
    data class Error(val message: String) : DownloadState
}

/** SEND_FROM_SERVER = this device pushes a real message OUT, through
 * outbox.py + a live platform SDK, to a real Telegram/Discord/Slack user
 * (POST /api/chat/send) — appears to that user as coming from the bot.
 * CHAT_WITH_BOT = a real message FROM this device TO the bot (POST
 * /api/chat/send-to-bot), through the exact same
 * CmdContext/dispatch_command/router.ask() pipeline every real Telegram/
 * Discord/Slack message goes through — nothing simulated, a genuine reply
 * comes back. Mirrors dashboard.html's Chat tab mode toggle — same two
 * modes, same backend endpoints, same always-visible-mode requirement.
 * Global to the screen, not per-instance, matching the dashboard's choice
 * for the same reason: exactly one place to look to know which seat you're
 * in. */
enum class ChatMode { SEND_FROM_SERVER, CHAT_WITH_BOT }

data class ChatUiState(
    val instances: List<BotInstanceSummary> = emptyList(),
    val activeInstanceId: Int? = null,
    val messages: List<ChatMessage> = emptyList(),
    val loadError: String? = null,
    val downloads: Map<Int, DownloadState> = emptyMap(),
    val sendFileError: String? = null,
    val sendingFile: Boolean = false,
    val uploadProgress: Float = 0f,
    val mode: ChatMode = ChatMode.SEND_FROM_SERVER,
)

@OptIn(ExperimentalCoroutinesApi::class)
@HiltViewModel
class ChatViewModel @Inject constructor(private val repository: ChatRepository) : ViewModel() {

    private val _uiState = MutableStateFlow(ChatUiState())
    val uiState: StateFlow<ChatUiState> = _uiState

    private var pollingStarted = false

    init {
        // Room is the source of truth for the active conversation's
        // messages — this re-subscribes whenever activeInstanceId changes,
        // so switching panels never needs to reach back into the network
        // to show what's already cached.
        _uiState
            .map { it.activeInstanceId }
            .flatMapLatest { id -> if (id == null) flowOf(emptyList()) else repository.observeMessages(id) }
            .onEach { messages -> _uiState.update { it.copy(messages = messages) } }
            .launchIn(viewModelScope)
    }

    fun start() {
        if (pollingStarted) return
        pollingStarted = true
        viewModelScope.launch {
            while (true) {
                refreshRecipients()
                _uiState.value.activeInstanceId?.let { repository.refreshMessages(it) }
                delay(2000)
            }
        }
    }

    private suspend fun refreshRecipients() {
        runCatching { repository.recipients() }
            .onSuccess { instances ->
                _uiState.update { s ->
                    val active = s.activeInstanceId ?: instances.firstOrNull()?.id
                    s.copy(instances = instances, activeInstanceId = active, loadError = null)
                }
            }
            .onFailure { e -> _uiState.update { it.copy(loadError = e.message) } }
    }

    fun switchInstance(instanceId: Int) {
        _uiState.update { it.copy(activeInstanceId = instanceId) }
        viewModelScope.launch { repository.refreshMessages(instanceId) }
    }

    fun setMode(mode: ChatMode) {
        _uiState.update { it.copy(mode = mode) }
    }

    fun send(text: String) {
        val instanceId = _uiState.value.activeInstanceId ?: return
        if (_uiState.value.mode == ChatMode.CHAT_WITH_BOT) {
            viewModelScope.launch {
                runCatching { repository.sendToBot(instanceId, text) }
                    .onSuccess { repository.refreshMessages(instanceId) }
            }
            return
        }
        val inst = _uiState.value.instances.find { it.id == instanceId } ?: return
        val chatId = inst.allowedIds.firstOrNull() ?: return
        viewModelScope.launch {
            runCatching { repository.send(instanceId, chatId, text) }
                .onSuccess { repository.refreshMessages(instanceId) }
        }
    }

    fun sendFile(uri: Uri, caption: String) {
        val instanceId = _uiState.value.activeInstanceId ?: return
        val inst = _uiState.value.instances.find { it.id == instanceId } ?: return
        val chatId = inst.allowedIds.firstOrNull() ?: return
        _uiState.update { it.copy(sendingFile = true, sendFileError = null, uploadProgress = 0f) }
        viewModelScope.launch {
            runCatching {
                repository.sendFile(instanceId, chatId, caption, uri) { progress ->
                    _uiState.update { it.copy(uploadProgress = progress) }
                }
            }
                .onSuccess { repository.refreshMessages(instanceId) }
                .onFailure { e -> _uiState.update { it.copy(sendFileError = e.message ?: "Couldn't send that file.") } }
            _uiState.update { it.copy(sendingFile = false, uploadProgress = 0f) }
        }
    }

    fun downloadAttachment(message: ChatMessage) {
        if (_uiState.value.downloads[message.id] is DownloadState.Downloading) return
        _uiState.update { it.copy(downloads = it.downloads + (message.id to DownloadState.Downloading)) }
        viewModelScope.launch {
            val result = runCatching { repository.downloadAttachment(message.id, message.attachmentName ?: "file") }
            val newState = result.fold(
                onSuccess = { DownloadState.Ready(it) },
                onFailure = { e -> DownloadState.Error(e.message ?: "Download failed.") },
            )
            _uiState.update { it.copy(downloads = it.downloads + (message.id to newState)) }
        }
    }
}

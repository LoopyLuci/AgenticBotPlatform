package com.botserver.mobile.ui.serverchat

import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.ServerChatRepository
import com.botserver.mobile.data.dto.ServerChatConversation
import com.botserver.mobile.data.dto.ServerChatMessage
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class ServerChatViewModel @Inject constructor(private val repository: ServerChatRepository) : ViewModel() {

    private val _myDeviceId = MutableStateFlow(0)
    val myDeviceId: StateFlow<Int> = _myDeviceId

    private val _conversations = MutableStateFlow<List<ServerChatConversation>>(emptyList())
    val conversations: StateFlow<List<ServerChatConversation>> = _conversations

    private val _activeConversationId = MutableStateFlow<Int?>(null)
    val activeConversationId: StateFlow<Int?> = _activeConversationId

    private val _messages = MutableStateFlow<List<ServerChatMessage>>(emptyList())
    val messages: StateFlow<List<ServerChatMessage>> = _messages

    private val _loadError = MutableStateFlow<String?>(null)
    val loadError: StateFlow<String?> = _loadError

    private var lastId = 0
    private var listStarted = false

    // Same reasoning as ChatViewModel's identical field — loadError is
    // only ever rendered on the conversation *list* screen, never inside
    // an open conversation, so a delete/clear failure (or even a quiet
    // success) inside one was previously invisible.
    private val _snackbarMessages = kotlinx.coroutines.flow.MutableSharedFlow<String>(extraBufferCapacity = 1)
    val snackbarMessages: kotlinx.coroutines.flow.SharedFlow<String> = _snackbarMessages

    fun start() {
        if (listStarted) return
        listStarted = true
        viewModelScope.launch {
            runCatching { repository.myDeviceId() }.onSuccess { _myDeviceId.value = it }
        }
        refreshConversations()
    }

    fun refreshConversations() {
        viewModelScope.launch {
            runCatching { repository.conversations() }
                .onSuccess { _conversations.value = it; _loadError.value = null }
                .onFailure { _loadError.value = it.message ?: "Couldn't load conversations." }
        }
    }

    fun openConversation(id: Int) {
        _activeConversationId.value = id
        _messages.value = emptyList()
        lastId = 0
        refreshMessages()
    }

    fun closeConversation() {
        _activeConversationId.value = null
        _messages.value = emptyList()
        lastId = 0
    }

    fun refreshMessages() {
        val id = _activeConversationId.value ?: return
        viewModelScope.launch {
            runCatching { repository.messages(id, afterId = lastId) }.onSuccess { rows ->
                if (rows.isNotEmpty()) {
                    _messages.value = _messages.value + rows
                    lastId = rows.maxOf { it.id }
                }
            }
        }
    }

    fun send(text: String) {
        val id = _activeConversationId.value ?: return
        if (text.isBlank()) return
        viewModelScope.launch {
            runCatching { repository.send(id, text) }.onSuccess {
                refreshMessages()
                refreshConversations()
            }
        }
    }

    fun sendFile(uri: Uri, text: String) {
        val id = _activeConversationId.value ?: return
        viewModelScope.launch {
            runCatching { repository.sendFile(id, text, uri) }.onSuccess {
                refreshMessages()
                refreshConversations()
            }
        }
    }

    suspend fun downloadAttachment(messageId: Int, suggestedName: String) =
        repository.downloadAttachment(messageId, suggestedName)

    /** Optimistic — removes the row from the visible list immediately
     * rather than waiting on a round trip, since the only way this can
     * fail server-side (this device didn't send it) can't happen from
     * this UI in the first place: the delete option is only ever shown
     * on the caller's own messages (see ServerChatScreen). */
    fun deleteMessage(message: ServerChatMessage) {
        _messages.value = _messages.value.filterNot { it.id == message.id }
        viewModelScope.launch {
            runCatching { repository.deleteMessage(message.id) }
                .onSuccess { _snackbarMessages.tryEmit("Message deleted") }
                .onFailure { _snackbarMessages.tryEmit(it.message ?: "Couldn't delete that message.") }
        }
    }

    fun clearActiveConversation() {
        val id = _activeConversationId.value
        if (id == null) {
            _snackbarMessages.tryEmit("Couldn't delete this chat — no active conversation.")
            return
        }
        _messages.value = emptyList()
        lastId = 0
        viewModelScope.launch {
            runCatching { repository.clearConversation(id) }
                .onSuccess { _snackbarMessages.tryEmit("Chat deleted") }
                .onFailure { _snackbarMessages.tryEmit(it.message ?: "Couldn't delete this conversation.") }
            refreshConversations()
        }
    }
}

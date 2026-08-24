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
}

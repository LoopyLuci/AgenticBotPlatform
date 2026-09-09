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
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
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

    // The approval currently being resolved (if any) — lets the card show
    // a spinner on just the button that was tapped rather than freezing
    // the whole conversation while the round trip is in flight.
    private val _resolvingApprovalId = MutableStateFlow<Int?>(null)
    val resolvingApprovalId: StateFlow<Int?> = _resolvingApprovalId

    // approval_id -> the outcome it was resolved with, kept client-side
    // only (the underlying server_chat_messages row never changes) so a
    // card doesn't keep offering Approve/Deny forever after it's already
    // been acted on in this session. Reset per-conversation in
    // openConversation()/closeConversation() below.
    private val _resolvedApprovals = MutableStateFlow<Map<Int, String>>(emptyMap())
    val resolvedApprovals: StateFlow<Map<Int, String>> = _resolvedApprovals

    private var lastId = 0
    private var listStarted = false

    // Guards refreshMessages() against overlapping calls — it's invoked
    // both by a periodic poll (every 2s while a conversation is open,
    // see ServerChatScreen) and directly after send()/sendFile(). Without
    // this, two overlapping calls can both read the same `lastId` before
    // either updates it, both fetch the same new row, and both append it
    // to `_messages`, producing two list entries with the same id — which
    // crashes the conversation LazyColumn's `key = { it.id }` with
    // "Key ... was already used." Confirmed via a real crash log after
    // sending a message immediately after opening a fresh conversation.
    private val refreshMutex = Mutex()

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
        _resolvedApprovals.value = emptyMap()
        lastId = 0
        refreshMessages()
    }

    fun closeConversation() {
        _activeConversationId.value = null
        _messages.value = emptyList()
        _resolvedApprovals.value = emptyMap()
        lastId = 0
    }

    fun refreshMessages() {
        val id = _activeConversationId.value ?: return
        viewModelScope.launch {
            refreshMutex.withLock {
                // The conversation may have changed (or closed) while
                // this call was waiting for the lock — don't apply a
                // stale fetch's results against a different one.
                if (_activeConversationId.value != id) return@withLock
                runCatching { repository.messages(id, afterId = lastId) }.onSuccess { rows ->
                    if (rows.isNotEmpty()) {
                        val existingIds = _messages.value.mapTo(HashSet()) { it.id }
                        val newRows = rows.filterNot { it.id in existingIds }
                        if (newRows.isNotEmpty()) _messages.value = _messages.value + newRows
                        lastId = rows.maxOf { it.id }
                    }
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

    /** The group room can only ever be cleared (see repository/backend
     * doc — a shared room can't be unilaterally removed for everyone);
     * a direct 1:1 conversation is genuinely deleted, matching "fully
     * and completely delete entire chats," not just its messages.
     * Works whether or not this conversation is the currently-open one —
     * the list screen's own long-press menu calls this directly against
     * a row's conversation without ever opening it first. */
    fun deleteConversation(conversation: ServerChatConversation) {
        val id = conversation.id
        val wasActive = _activeConversationId.value == id
        if (wasActive) {
            _messages.value = emptyList()
            lastId = 0
        }
        val isGroup = conversation.kind == "group"
        viewModelScope.launch {
            runCatching {
                if (isGroup) repository.clearConversation(id) else repository.deleteConversation(id)
            }
                .onSuccess {
                    _snackbarMessages.tryEmit(if (isGroup) "Chat cleared" else "Chat deleted")
                    if (!isGroup && wasActive) closeConversation()
                }
                .onFailure { _snackbarMessages.tryEmit(it.message ?: "Couldn't delete this conversation.") }
            refreshConversations()
        }
    }

    /** Resolves a pending dangerous-tool approval rendered from an
     * approval_request message — [outcome] is "once" | "session" |
     * "always" | "deny". A 409 (already resolved elsewhere, or timed
     * out) is surfaced via the snackbar rather than silently ignored,
     * since the card would otherwise look actionable forever. */
    fun resolveApproval(approvalId: Int, outcome: String) {
        _resolvingApprovalId.value = approvalId
        viewModelScope.launch {
            runCatching { repository.resolveApproval(approvalId, outcome) }
                .onSuccess {
                    _snackbarMessages.tryEmit(if (outcome == "deny") "Denied." else "Approved.")
                    _resolvedApprovals.value = _resolvedApprovals.value + (approvalId to outcome)
                    refreshMessages()
                }
                .onFailure { e -> _snackbarMessages.tryEmit(e.message ?: "Couldn't resolve that request — it may have already timed out.") }
            _resolvingApprovalId.value = null
        }
    }

    /** Opens (or re-opens, after a full delete) a direct conversation
     * with another device and switches straight to it — the entry point
     * used from the Devices screen's "Message this device" action. */
    fun openConversationWith(peerDeviceId: Int) {
        viewModelScope.launch {
            runCatching { repository.openConversation(peerDeviceId) }
                .onSuccess { conversationId ->
                    refreshConversations()
                    openConversation(conversationId)
                }
                .onFailure { _snackbarMessages.tryEmit(it.message ?: "Couldn't open a conversation with that device.") }
        }
    }
}

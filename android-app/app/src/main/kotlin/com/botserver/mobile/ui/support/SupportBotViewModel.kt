package com.botserver.mobile.ui.support

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.SupportBotRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

data class SupportBotMessage(
    val text: String,
    val direction: String, // "in" | "out"
    val needsConfirm: Boolean = false,
    val confirmToken: String? = null,
    val confirmResolved: Boolean = false,
)

data class SupportBotUiState(
    val messages: List<SupportBotMessage> = listOf(
        SupportBotMessage(
            text = "Ask me things like \"what's the status\", \"restart the discord bot\", " +
                "\"enable the filesystem mcp server\", or type \"/\" for the command list.",
            direction = "in",
        ),
    ),
    val sending: Boolean = false,
)

/** Local, in-memory chat state — mirrors the desktop dashboard's Support
 * Bot panel (no message history endpoint on the server, since the model
 * is stateless request/reply plus a short-lived confirm token). */
@HiltViewModel
class SupportBotViewModel @Inject constructor(private val repository: SupportBotRepository) : ViewModel() {

    private val _uiState = MutableStateFlow(SupportBotUiState())
    val uiState: StateFlow<SupportBotUiState> = _uiState

    fun send(text: String) {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return
        _uiState.update { it.copy(messages = it.messages + SupportBotMessage(trimmed, "out"), sending = true) }
        viewModelScope.launch {
            val reply = runCatching { repository.ask(trimmed) }
            _uiState.update { state ->
                val bubble = reply.fold(
                    onSuccess = { r -> SupportBotMessage(r.text, "in", r.needsConfirm, r.confirmToken) },
                    onFailure = { e -> SupportBotMessage("Error: ${e.message}", "in") },
                )
                state.copy(messages = state.messages + bubble, sending = false)
            }
        }
    }

    fun confirm(message: SupportBotMessage) {
        val token = message.confirmToken ?: return
        viewModelScope.launch {
            _uiState.update { state ->
                state.copy(messages = state.messages.map { if (it === message) it.copy(confirmResolved = true) else it })
            }
            val reply = runCatching { repository.confirm(token) }
            _uiState.update { state ->
                val bubble = reply.fold(
                    onSuccess = { r -> SupportBotMessage(r.text, "in") },
                    onFailure = { e -> SupportBotMessage("Confirm failed: ${e.message}", "in") },
                )
                state.copy(messages = state.messages + bubble)
            }
        }
    }
}

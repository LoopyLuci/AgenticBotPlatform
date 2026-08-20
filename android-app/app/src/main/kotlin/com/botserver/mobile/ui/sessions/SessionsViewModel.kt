package com.botserver.mobile.ui.sessions

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.SessionsRepository
import com.botserver.mobile.data.dto.SessionDetail
import com.botserver.mobile.data.dto.SessionSummary
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

data class SessionsUiState(
    val sessions: List<SessionSummary> = emptyList(),
    val selected: SessionDetail? = null,
    val loading: Boolean = false,
    val error: String? = null,
)

@HiltViewModel
class SessionsViewModel @Inject constructor(private val repository: SessionsRepository) : ViewModel() {

    private val _uiState = MutableStateFlow(SessionsUiState())
    val uiState: StateFlow<SessionsUiState> = _uiState

    fun refresh() {
        viewModelScope.launch {
            _uiState.update { it.copy(loading = true) }
            runCatching { repository.list() }
                .onSuccess { list -> _uiState.update { it.copy(sessions = list, loading = false, error = null) } }
                .onFailure { e -> _uiState.update { it.copy(loading = false, error = e.message) } }
        }
    }

    fun open(sessionId: String) {
        viewModelScope.launch {
            runCatching { repository.detail(sessionId) }
                .onSuccess { detail -> _uiState.update { it.copy(selected = detail) } }
        }
    }

    fun closeDetail() {
        _uiState.update { it.copy(selected = null) }
    }
}

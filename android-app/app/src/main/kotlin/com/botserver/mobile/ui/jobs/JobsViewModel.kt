package com.botserver.mobile.ui.jobs

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.JobsRepository
import com.botserver.mobile.data.dto.JobSummary
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

data class JobsUiState(val jobs: List<JobSummary> = emptyList(), val loading: Boolean = true, val error: String? = null)

@HiltViewModel
class JobsViewModel @Inject constructor(private val repository: JobsRepository) : ViewModel() {

    private val _uiState = MutableStateFlow(JobsUiState())
    val uiState: StateFlow<JobsUiState> = _uiState
    private var polling = false

    fun refreshNow() {
        viewModelScope.launch {
            runCatching { repository.list() }
                .onSuccess { list -> _uiState.update { it.copy(jobs = list, error = null, loading = false) } }
                .onFailure { e -> _uiState.update { it.copy(error = e.message, loading = false) } }
        }
    }

    fun start() {
        if (polling) return
        polling = true
        viewModelScope.launch {
            while (true) {
                runCatching { repository.list() }
                    .onSuccess { list -> _uiState.update { it.copy(jobs = list, error = null, loading = false) } }
                    .onFailure { e -> _uiState.update { it.copy(error = e.message, loading = false) } }
                delay(5000)
            }
        }
    }
}

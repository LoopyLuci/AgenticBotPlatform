package com.botserver.mobile.ui.jobs

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.JobsRepository
import com.botserver.mobile.data.dto.JobSummary
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

data class JobsUiState(val jobs: List<JobSummary> = emptyList(), val loading: Boolean = true, val error: String? = null)

@HiltViewModel
class JobsViewModel @Inject constructor(private val repository: JobsRepository) : ViewModel() {

    private val _uiState = MutableStateFlow(JobsUiState())
    val uiState: StateFlow<JobsUiState> = _uiState
    private var polling = false

    init {
        // Real-time: a "job_update" push upserts directly into the
        // in-memory list — no need to wait for the next poll to see a
        // job go running -> success. Jobs aren't Room-cached (unlike Chat)
        // since there's no equivalent unbounded-growth problem here — the
        // list is always a fresh, bounded GET /api/jobs fetch — so this
        // is a plain in-memory upsert rather than a cache write.
        repository.observeLiveUpdates()
            .onEach { job ->
                _uiState.update { state ->
                    val exists = state.jobs.any { it.id == job.id }
                    val updated = if (exists) state.jobs.map { if (it.id == job.id) job else it } else listOf(job) + state.jobs
                    state.copy(jobs = updated)
                }
            }
            .launchIn(viewModelScope)
    }

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
                delay(60_000) // reconciliation backstop — job_update pushes above drive the live UI
            }
        }
    }
}

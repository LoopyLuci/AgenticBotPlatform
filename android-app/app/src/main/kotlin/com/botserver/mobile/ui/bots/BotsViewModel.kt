package com.botserver.mobile.ui.bots

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.BotsRepository
import com.botserver.mobile.data.dto.BotInstance
import com.botserver.mobile.data.dto.PairingRequest
import com.botserver.mobile.data.dto.PersonaPreset
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

data class BotForm(
    val editingId: Int? = null,
    val name: String = "",
    val platform: String = "telegram",
    val backend: String = "cli",
    val model: String = "",
    val persona: String = "assistant",
    val botToken: String = "",
    val appToken: String = "",
    val allowedIds: String = "",
    val enabled: Boolean = true,
    val saving: Boolean = false,
    val loadingCredentials: Boolean = false,
    val error: String? = null,
) {
    val isEditing get() = editingId != null
}

data class BotsUiState(
    val bots: List<BotInstance> = emptyList(),
    val personas: List<PersonaPreset> = emptyList(),
    val pendingPairings: List<PairingRequest> = emptyList(),
    val loading: Boolean = true,
    val error: String? = null,
    val form: BotForm? = null,
    val busyId: Int? = null,
    val busyPairingId: Int? = null,
)

@HiltViewModel
class BotsViewModel @Inject constructor(private val repository: BotsRepository) : ViewModel() {

    private val _uiState = MutableStateFlow(BotsUiState())
    val uiState: StateFlow<BotsUiState> = _uiState

    init {
        // Room is the source of truth for the list — this collects it for
        // as long as the ViewModel lives, independent of how many times
        // refresh() below is called to repopulate it.
        repository.observeBots().onEach { list -> _uiState.update { it.copy(bots = list) } }.launchIn(viewModelScope)
    }

    fun refresh() {
        viewModelScope.launch {
            repository.refresh()
                .onSuccess { _uiState.update { it.copy(error = null, loading = false) } }
                .onFailure { e -> _uiState.update { it.copy(error = e.message, loading = false) } }
        }
        if (_uiState.value.personas.isEmpty()) {
            viewModelScope.launch {
                runCatching { repository.personas() }.onSuccess { list -> _uiState.update { it.copy(personas = list) } }
            }
        }
        viewModelScope.launch {
            runCatching { repository.pendingPairings() }
                .onSuccess { list -> _uiState.update { it.copy(pendingPairings = list) } }
        }
    }

    fun approvePairing(request: PairingRequest) {
        viewModelScope.launch {
            _uiState.update { it.copy(busyPairingId = request.id) }
            runCatching { repository.approvePairing(request.id) }.onSuccess { refresh() }
            _uiState.update { it.copy(busyPairingId = null) }
        }
    }

    fun denyPairing(request: PairingRequest) {
        viewModelScope.launch {
            _uiState.update { it.copy(busyPairingId = request.id) }
            runCatching { repository.denyPairing(request.id) }.onSuccess { refresh() }
            _uiState.update { it.copy(busyPairingId = null) }
        }
    }

    fun toggleEnabled(bot: BotInstance) {
        viewModelScope.launch {
            _uiState.update { it.copy(busyId = bot.id) }
            runCatching { repository.toggleEnabled(bot) }.onSuccess { refresh() }
            _uiState.update { it.copy(busyId = null) }
        }
    }

    fun toggleRunning(bot: BotInstance) {
        viewModelScope.launch {
            _uiState.update { it.copy(busyId = bot.id) }
            runCatching { repository.toggleRunning(bot) }.onSuccess { refresh() }
            _uiState.update { it.copy(busyId = null) }
        }
    }

    fun restart(bot: BotInstance) {
        viewModelScope.launch {
            _uiState.update { it.copy(busyId = bot.id) }
            runCatching { repository.restart(bot.id) }.onSuccess { refresh() }
            _uiState.update { it.copy(busyId = null) }
        }
    }

    fun delete(bot: BotInstance) {
        viewModelScope.launch {
            _uiState.update { it.copy(busyId = bot.id) }
            runCatching { repository.delete(bot.id) }.onSuccess { refresh() }
            _uiState.update { it.copy(busyId = null) }
        }
    }

    fun startCreate() {
        _uiState.update { it.copy(form = BotForm()) }
    }

    /** The list's own [bot] never carries a real credential (see
     * BotsRepository's doc on why Room excludes it) — this fetches it
     * fresh from the network the moment the (already biometric-gated) edit
     * form opens, rather than reading a cached one that doesn't exist. */
    fun startEdit(bot: BotInstance) {
        _uiState.update {
            it.copy(
                form = BotForm(
                    editingId = bot.id,
                    name = bot.name,
                    platform = bot.platform,
                    backend = bot.backend,
                    model = bot.model ?: "",
                    persona = bot.persona,
                    allowedIds = bot.allowedUserIds.joinToString(", "),
                    enabled = bot.enabled,
                    loadingCredentials = true,
                ),
            )
        }
        viewModelScope.launch {
            runCatching { repository.getForEdit(bot.id) }
                .onSuccess { fresh ->
                    updateForm {
                        it.copy(
                            botToken = fresh.credentials.botToken ?: "",
                            appToken = fresh.credentials.appToken ?: "",
                            loadingCredentials = false,
                        )
                    }
                }
                .onFailure { e ->
                    updateForm { it.copy(error = "Couldn't load this bot's credentials: ${e.message}", loadingCredentials = false) }
                }
        }
    }

    fun updateForm(transform: (BotForm) -> BotForm) {
        _uiState.update { it.copy(form = it.form?.let(transform)) }
    }

    fun cancelForm() {
        _uiState.update { it.copy(form = null) }
    }

    fun saveForm() {
        val form = _uiState.value.form ?: return
        if (form.name.isBlank()) {
            updateForm { it.copy(error = "Name can't be empty.") }
            return
        }
        if (form.botToken.isBlank()) {
            updateForm { it.copy(error = "Bot token can't be empty.") }
            return
        }
        val allowedIds = form.allowedIds.split(",").map { it.trim() }.filter { it.isNotEmpty() }
        updateForm { it.copy(saving = true, error = null) }
        viewModelScope.launch {
            val result = runCatching {
                if (form.isEditing) {
                    repository.update(
                        id = form.editingId!!,
                        name = form.name,
                        platform = form.platform,
                        backend = form.backend,
                        model = form.model,
                        persona = form.persona,
                        botToken = form.botToken,
                        appToken = form.appToken,
                        allowedUserIds = allowedIds,
                        enabled = form.enabled,
                    )
                } else {
                    repository.create(
                        name = form.name,
                        platform = form.platform,
                        backend = form.backend,
                        model = form.model,
                        persona = form.persona,
                        botToken = form.botToken,
                        appToken = form.appToken,
                        allowedUserIds = allowedIds,
                    )
                }
            }
            result.onSuccess {
                _uiState.update { it.copy(form = null) }
                refresh()
            }.onFailure { e ->
                updateForm { it.copy(saving = false, error = e.message ?: "Couldn't save this bot.") }
            }
        }
    }
}

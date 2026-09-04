package com.botserver.mobile.ui.bots

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.BotsRepository
import com.botserver.mobile.data.dto.BotCredentials
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
    // Reused across platforms as whichever single secret token that
    // platform's own credential field is — Bot token for Telegram/
    // Discord/Slack, Access token for Matrix/WhatsApp — mirroring the
    // desktop dashboard's own "one relabeled field" pattern rather than
    // a separate accessToken field that would just duplicate this one.
    val botToken: String = "",
    val appToken: String = "",
    val homeserver: String = "",
    val matrixUserId: String = "",
    val matrixDeviceId: String = "",
    val whatsappPhoneNumberId: String = "",
    val whatsappAppSecret: String = "",
    val whatsappVerifyToken: String = "",
    val allowedIds: String = "",
    val enabled: Boolean = true,
    val saving: Boolean = false,
    val loadingCredentials: Boolean = false,
    val error: String? = null,
) {
    val isEditing get() = editingId != null

    /** The label the single reused token field should show for this
     * platform — matches the desktop dashboard's own relabeling. */
    val tokenFieldLabel: String
        get() = if (platform == "matrix" || platform == "whatsapp") "Access token" else "Bot token"
}

/** Builds the real bot_instances.py credentials JSON for [form]'s current
 * platform — the one place that knows which of the form's fields maps to
 * which platform, so saveForm()/startEdit() don't each need their own
 * copy of this mapping. Blank optional fields become null rather than
 * empty strings, matching bot.bot_instances._validate_credentials()'s
 * "falsy means unset" checks. */
private fun BotForm.toCredentials(): BotCredentials = when (platform) {
    "slack" -> BotCredentials(botToken = botToken, appToken = appToken.takeIf { it.isNotBlank() })
    "matrix" -> BotCredentials(
        homeserver = homeserver.trim(),
        userId = matrixUserId.trim(),
        accessToken = botToken,
        deviceId = matrixDeviceId.trim().takeIf { it.isNotBlank() },
    )
    "whatsapp" -> BotCredentials(
        phoneNumberId = whatsappPhoneNumberId.trim(),
        accessToken = botToken,
        appSecret = whatsappAppSecret.trim(),
        verifyToken = whatsappVerifyToken.trim(),
    )
    else -> BotCredentials(botToken = botToken)
}

/** null when [form]'s platform-specific required fields are all present;
 * otherwise the message to show. Mirrors bot.validators.PLATFORM_TOKEN_VALIDATORS'
 * per-platform required-field list (this only checks non-blank — the
 * server's own format validators are the authority on shape). */
private fun BotForm.missingCredentialField(): String? = when (platform) {
    "telegram", "discord" -> if (botToken.isBlank()) "Bot token can't be empty." else null
    "slack" -> when {
        botToken.isBlank() -> "Bot token can't be empty."
        appToken.isBlank() -> "App token can't be empty."
        else -> null
    }
    "matrix" -> when {
        homeserver.isBlank() -> "Homeserver can't be empty."
        matrixUserId.isBlank() -> "Matrix user ID can't be empty."
        botToken.isBlank() -> "Access token can't be empty."
        else -> null
    }
    "whatsapp" -> when {
        whatsappPhoneNumberId.isBlank() -> "Phone Number ID can't be empty."
        botToken.isBlank() -> "Access token can't be empty."
        whatsappAppSecret.isBlank() -> "App secret can't be empty."
        whatsappVerifyToken.isBlank() -> "Verify token can't be empty."
        else -> null
    }
    else -> if (botToken.isBlank()) "Bot token can't be empty." else null
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
                    val creds = fresh.credentials
                    updateForm {
                        it.copy(
                            // Matrix/WhatsApp store their one secret token
                            // under access_token, not bot_token — see
                            // BotForm.tokenFieldLabel/toCredentials above.
                            botToken = (if (fresh.platform == "matrix" || fresh.platform == "whatsapp") creds.accessToken else creds.botToken) ?: "",
                            appToken = creds.appToken ?: "",
                            homeserver = creds.homeserver ?: "",
                            matrixUserId = creds.userId ?: "",
                            matrixDeviceId = creds.deviceId ?: "",
                            whatsappPhoneNumberId = creds.phoneNumberId ?: "",
                            whatsappAppSecret = creds.appSecret ?: "",
                            whatsappVerifyToken = creds.verifyToken ?: "",
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
        form.missingCredentialField()?.let { message ->
            updateForm { it.copy(error = message) }
            return
        }
        val allowedIds = form.allowedIds.split(",").map { it.trim() }.filter { it.isNotEmpty() }
        val credentials = form.toCredentials()
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
                        credentials = credentials,
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
                        credentials = credentials,
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

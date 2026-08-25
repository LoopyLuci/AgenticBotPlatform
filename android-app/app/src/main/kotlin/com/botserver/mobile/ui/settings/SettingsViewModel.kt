package com.botserver.mobile.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.SettingsRepository
import com.botserver.mobile.data.dto.agentControlMode
import com.botserver.mobile.data.dto.confirmDestructive
import com.botserver.mobile.data.dto.defaultBackend
import com.botserver.mobile.data.dto.defaultHermesBackend
import com.botserver.mobile.data.dto.uiAutomationEnabled
import com.botserver.mobile.data.dto.verboseTelemetry
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

data class SettingsUiState(
    val loading: Boolean = true,
    val error: String? = null,
    val defaultBackend: String = "cli",
    val defaultHermesBackend: String = "hermes_gateway",
    val agentControlMode: String = "trust_all",
    val uiAutomationEnabled: Boolean = true,
    val confirmDestructive: Boolean = true,
    val verboseTelemetry: Boolean = true,
    val knownApiModels: List<String> = emptyList(),
    val apiModel: String = "",
    val hermesCliModel: String = "",
    val hermesGatewayModel: String = "",
    val savingKey: String? = null,
)

/** Mirrors the desktop dashboard's Control Center — same underlying
 * POST /api/config/set the desktop uses, so a change made from the phone
 * is a change to the one config file the server (and every other
 * connected device) reads from — there's no separate mobile copy to fall
 * out of sync. */
@HiltViewModel
class SettingsViewModel @Inject constructor(private val repository: SettingsRepository) : ViewModel() {

    private val _uiState = MutableStateFlow(SettingsUiState())
    val uiState: StateFlow<SettingsUiState> = _uiState

    fun refresh() {
        viewModelScope.launch {
            val configResult = runCatching { repository.config() }
            val modelsResult = runCatching { repository.models() }
            val config = configResult.getOrNull()
            val models = modelsResult.getOrNull()
            if (config == null) {
                _uiState.update { it.copy(loading = false, error = configResult.exceptionOrNull()?.message) }
                return@launch
            }
            _uiState.update {
                it.copy(
                    loading = false,
                    error = null,
                    defaultBackend = config.defaultBackend ?: it.defaultBackend,
                    defaultHermesBackend = config.defaultHermesBackend ?: it.defaultHermesBackend,
                    agentControlMode = config.agentControlMode ?: it.agentControlMode,
                    uiAutomationEnabled = config.uiAutomationEnabled ?: it.uiAutomationEnabled,
                    confirmDestructive = config.confirmDestructive ?: it.confirmDestructive,
                    verboseTelemetry = config.verboseTelemetry ?: it.verboseTelemetry,
                    knownApiModels = models?.known?.get("api") ?: it.knownApiModels,
                    apiModel = models?.current?.get("api") ?: it.apiModel,
                    hermesCliModel = models?.current?.get("hermes_cli") ?: it.hermesCliModel,
                    hermesGatewayModel = models?.current?.get("hermes_gateway") ?: it.hermesGatewayModel,
                )
            }
        }
    }

    private fun <T> apply(key: String, optimistic: (SettingsUiState) -> SettingsUiState, write: suspend () -> T) {
        _uiState.update { optimistic(it).copy(savingKey = key) }
        viewModelScope.launch {
            runCatching { write() }
                .onFailure { e -> _uiState.update { it.copy(error = e.message ?: "Couldn't save that setting.") } }
            _uiState.update { it.copy(savingKey = null) }
            refresh()
        }
    }

    fun setDefaultBackend(backend: String) =
        apply("default_backend", { it.copy(defaultBackend = backend) }) { repository.setDefaultBackend(backend) }

    fun setDefaultHermesBackend(backend: String) =
        apply("default_hermes_backend", { it.copy(defaultHermesBackend = backend) }) { repository.setDefaultHermesBackend(backend) }

    fun setAgentControlMode(mode: String) =
        apply("agent_control", { it.copy(agentControlMode = mode) }) { repository.setAgentControlMode(mode) }

    fun setApiModel(model: String) =
        apply("model_api", { it.copy(apiModel = model) }) { repository.setModel("api", model.ifBlank { null }) }

    fun setHermesCliModel(model: String) =
        apply("model_hermes_cli", { it.copy(hermesCliModel = model) }) { repository.setModel("hermes_cli", model.ifBlank { null }) }

    fun setHermesGatewayModel(model: String) =
        apply("model_hermes_gateway", { it.copy(hermesGatewayModel = model) }) { repository.setModel("hermes_gateway", model.ifBlank { null }) }

    fun setUiAutomationEnabled(enabled: Boolean) =
        apply("ui_automation", { it.copy(uiAutomationEnabled = enabled) }) { repository.setUiAutomationEnabled(enabled) }

    fun setConfirmDestructive(enabled: Boolean) =
        apply("confirm_destructive", { it.copy(confirmDestructive = enabled) }) { repository.setConfirmDestructive(enabled) }

    fun setVerboseTelemetry(enabled: Boolean) =
        apply("verbose_telemetry", { it.copy(verboseTelemetry = enabled) }) { repository.setVerboseTelemetry(enabled) }
}

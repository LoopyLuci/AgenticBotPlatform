package com.botserver.mobile.ui.automation

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.AutomationRepository
import com.botserver.mobile.data.dto.AgentSettings
import com.botserver.mobile.data.dto.AutoManageConfig
import com.botserver.mobile.data.dto.BotInstance
import com.botserver.mobile.data.dto.HookInfo
import com.botserver.mobile.data.dto.SetAgentSettingsRequest
import com.botserver.mobile.data.dto.SetAutoManageRequest
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

data class NewHookForm(
    val event: String = "PreToolUse",
    val matcher: String = "",
    val instanceId: Int? = null,
    val command: String = "",
)

data class AgentSettingsForm(
    val maxConcurrentChildren: String = "",
    val workerProvider: String = "",
    val workerModel: String = "",
    val workerEffort: String = "",
    val managerEffort: String = "",
    val fallbackProvider: String = "",
    val fallbackModel: String = "",
    val requirePlanApproval: Boolean = false,
    val isAdminInstance: Boolean = false,
) {
    companion object {
        fun from(s: AgentSettings) = AgentSettingsForm(
            maxConcurrentChildren = s.maxConcurrentChildren?.toString() ?: "",
            workerProvider = s.workerProvider ?: "",
            workerModel = s.workerModel ?: "",
            workerEffort = s.workerEffort ?: "",
            managerEffort = s.managerEffort ?: "",
            fallbackProvider = s.fallbackProvider ?: "",
            fallbackModel = s.fallbackModel ?: "",
            requirePlanApproval = s.requirePlanApproval,
            isAdminInstance = s.isAdminInstance,
        )
    }
}

data class AutoManageForm(
    val trigger: String = "scheduled",
    val interval: String = "30m",
    val chatId: String = "",
    val threadId: String = "",
    val goalTemplate: String = "",
) {
    companion object {
        fun from(c: AutoManageConfig) = AutoManageForm(
            trigger = c.trigger ?: "scheduled",
            interval = c.interval ?: "30m",
            chatId = c.chatId ?: "",
            threadId = c.threadId ?: "",
            goalTemplate = c.goalTemplate ?: "",
        )
    }
}

data class AutomationUiState(
    val loading: Boolean = true,
    val error: String? = null,
    val instances: List<BotInstance> = emptyList(),

    val hooks: List<HookInfo> = emptyList(),
    val newHook: NewHookForm = NewHookForm(),
    val addingHook: Boolean = false,
    val hookStatus: String? = null,

    val agentSettingsInstanceId: Int? = null, // null = process-wide default
    val agentSettingsForm: AgentSettingsForm = AgentSettingsForm(),
    val savingAgentSettings: Boolean = false,
    val agentSettingsStatus: String? = null,

    val autoManageInstanceId: Int? = null,
    val autoManageForm: AutoManageForm = AutoManageForm(),
    val autoManageEnabled: Boolean = false,
    val savingAutoManage: Boolean = false,
    val togglingAutoManage: Boolean = false,
    val autoManageStatus: String? = null,
)

/** Android parity with the dashboard/desktop "Automation" section — hooks,
 * agent_settings, and auto_manage all already had full backend routes and
 * MCP tool exposure (Claude Desktop could already read/write them) but no
 * mobile UI until now. See bot/agent_runtime/hooks.py, bot/agent_settings.py,
 * bot/auto_manage.py. */
@HiltViewModel
class AutomationViewModel @Inject constructor(private val repository: AutomationRepository) : ViewModel() {

    private val _uiState = MutableStateFlow(AutomationUiState())
    val uiState: StateFlow<AutomationUiState> = _uiState

    fun refresh() {
        viewModelScope.launch {
            _uiState.update { it.copy(loading = it.instances.isEmpty(), error = null) }
            val instancesResult = runCatching { repository.instances() }
            val hooksResult = runCatching { repository.hooks() }
            val agentSettingsResult = runCatching { repository.agentSettings(_uiState.value.agentSettingsInstanceId) }
            _uiState.update {
                it.copy(
                    loading = false,
                    error = instancesResult.exceptionOrNull()?.message
                        ?: hooksResult.exceptionOrNull()?.message
                        ?: agentSettingsResult.exceptionOrNull()?.message,
                    instances = instancesResult.getOrNull() ?: it.instances,
                    hooks = hooksResult.getOrNull() ?: it.hooks,
                    agentSettingsForm = agentSettingsResult.getOrNull()?.let(AgentSettingsForm::from) ?: it.agentSettingsForm,
                )
            }
            _uiState.value.autoManageInstanceId?.let(::loadAutoManage)
        }
    }

    // ----------------------------------------------------------- hooks ---
    fun updateNewHook(transform: (NewHookForm) -> NewHookForm) = _uiState.update { it.copy(newHook = transform(it.newHook), hookStatus = null) }

    fun addHook() {
        val form = _uiState.value.newHook
        if (form.command.isBlank()) {
            _uiState.update { it.copy(hookStatus = "A command is required.") }
            return
        }
        _uiState.update { it.copy(addingHook = true, hookStatus = null) }
        viewModelScope.launch {
            runCatching {
                repository.addHook(form.event, form.command.trim(), form.matcher.trim().ifBlank { null }, form.instanceId)
            }.onSuccess {
                _uiState.update { it.copy(addingHook = false, hookStatus = "Hook added.", newHook = NewHookForm()) }
                loadHooks()
            }.onFailure { e ->
                _uiState.update { it.copy(addingHook = false, hookStatus = e.message ?: "Couldn't add that hook.") }
            }
        }
    }

    fun setHookEnabled(hook: HookInfo, enabled: Boolean) {
        // Optimistic, matching ProvidersViewModel.toggleModel's own shape —
        // reverted on failure rather than waiting on a round trip for a
        // toggle this simple.
        _uiState.update { it.copy(hooks = it.hooks.map { h -> if (h.id == hook.id) h.copy(enabled = enabled) else h }) }
        viewModelScope.launch {
            runCatching { repository.setHookEnabled(hook.id, enabled) }.onFailure { e ->
                _uiState.update {
                    it.copy(
                        hooks = it.hooks.map { h -> if (h.id == hook.id) h.copy(enabled = !enabled) else h },
                        hookStatus = e.message ?: "Couldn't update that hook.",
                    )
                }
            }
        }
    }

    fun removeHook(hook: HookInfo) {
        viewModelScope.launch {
            runCatching { repository.removeHook(hook.id) }
                .onSuccess { _uiState.update { it.copy(hooks = it.hooks.filterNot { h -> h.id == hook.id }) } }
                .onFailure { e -> _uiState.update { it.copy(hookStatus = e.message ?: "Couldn't remove that hook.") } }
        }
    }

    private fun loadHooks() {
        viewModelScope.launch {
            runCatching { repository.hooks() }.onSuccess { hooks -> _uiState.update { it.copy(hooks = hooks) } }
        }
    }

    // --------------------------------------------------- agent settings ---
    fun selectAgentSettingsInstance(instanceId: Int?) {
        _uiState.update { it.copy(agentSettingsInstanceId = instanceId, agentSettingsStatus = null) }
        viewModelScope.launch {
            runCatching { repository.agentSettings(instanceId) }
                .onSuccess { s -> _uiState.update { it.copy(agentSettingsForm = AgentSettingsForm.from(s)) } }
                .onFailure { e -> _uiState.update { it.copy(agentSettingsStatus = e.message ?: "Couldn't load agent settings.") } }
        }
    }

    fun updateAgentSettingsForm(transform: (AgentSettingsForm) -> AgentSettingsForm) =
        _uiState.update { it.copy(agentSettingsForm = transform(it.agentSettingsForm), agentSettingsStatus = null) }

    fun saveAgentSettings() {
        val state = _uiState.value
        val form = state.agentSettingsForm
        _uiState.update { it.copy(savingAgentSettings = true, agentSettingsStatus = null) }
        viewModelScope.launch {
            runCatching {
                repository.setAgentSettings(
                    SetAgentSettingsRequest(
                        instanceId = state.agentSettingsInstanceId,
                        maxConcurrentChildren = form.maxConcurrentChildren.trim().toIntOrNull(),
                        workerProvider = form.workerProvider.trim().ifBlank { null },
                        workerModel = form.workerModel.trim().ifBlank { null },
                        workerEffort = form.workerEffort.ifBlank { null },
                        managerEffort = form.managerEffort.ifBlank { null },
                        fallbackProvider = form.fallbackProvider.trim().ifBlank { null },
                        fallbackModel = form.fallbackModel.trim().ifBlank { null },
                        requirePlanApproval = form.requirePlanApproval,
                        isAdminInstance = form.isAdminInstance,
                    ),
                )
            }.onSuccess { s ->
                _uiState.update { it.copy(savingAgentSettings = false, agentSettingsStatus = "Saved.", agentSettingsForm = AgentSettingsForm.from(s)) }
            }.onFailure { e ->
                _uiState.update { it.copy(savingAgentSettings = false, agentSettingsStatus = e.message ?: "Couldn't save agent settings.") }
            }
        }
    }

    // ----------------------------------------------------- auto-manage ---
    fun selectAutoManageInstance(instanceId: Int?) {
        _uiState.update { it.copy(autoManageInstanceId = instanceId, autoManageStatus = null) }
        instanceId?.let(::loadAutoManage)
    }

    private fun loadAutoManage(instanceId: Int) {
        viewModelScope.launch {
            runCatching { repository.autoManage(instanceId) }
                .onSuccess { c -> _uiState.update { it.copy(autoManageForm = AutoManageForm.from(c), autoManageEnabled = c.enabled) } }
                .onFailure { e -> _uiState.update { it.copy(autoManageStatus = e.message ?: "Couldn't load auto-manage config.") } }
        }
    }

    fun updateAutoManageForm(transform: (AutoManageForm) -> AutoManageForm) =
        _uiState.update { it.copy(autoManageForm = transform(it.autoManageForm), autoManageStatus = null) }

    /** The enabled toggle saves immediately — unlike every other field
     * here, it has a real side effect (creates/removes a live
     * scheduled_commands row via bot/auto_manage.py's enable()/disable()),
     * so its on-screen state should never lie about whether a schedule
     * actually exists right now, the way waiting for a separate "Save"
     * tap would let it. */
    fun setAutoManageEnabled(enabled: Boolean) {
        val state = _uiState.value
        val instanceId = state.autoManageInstanceId ?: return
        if (enabled && state.autoManageForm.chatId.isBlank()) {
            _uiState.update { it.copy(autoManageStatus = "A chat id is required before enabling (where check-ins get delivered).") }
            return
        }
        _uiState.update { it.copy(togglingAutoManage = true, autoManageStatus = null) }
        viewModelScope.launch {
            val form = state.autoManageForm
            runCatching {
                repository.setAutoManage(
                    instanceId,
                    if (enabled) {
                        SetAutoManageRequest(
                            enabled = true, chatId = form.chatId.trim(), threadId = form.threadId.trim().ifBlank { null },
                            trigger = form.trigger, interval = form.interval.trim().ifBlank { "30m" },
                            goalTemplate = form.goalTemplate.trim().ifBlank { null },
                        )
                    } else {
                        SetAutoManageRequest(enabled = false)
                    },
                )
            }.onSuccess { c ->
                _uiState.update { it.copy(togglingAutoManage = false, autoManageEnabled = c.enabled, autoManageStatus = if (enabled) "Enabled." else "Disabled.") }
            }.onFailure { e ->
                _uiState.update { it.copy(togglingAutoManage = false, autoManageStatus = e.message ?: "Couldn't update auto-manage.") }
            }
        }
    }

    fun saveAutoManageSettings() {
        val state = _uiState.value
        val instanceId = state.autoManageInstanceId
        if (instanceId == null) {
            _uiState.update { it.copy(autoManageStatus = "Pick an instance first.") }
            return
        }
        val form = state.autoManageForm
        _uiState.update { it.copy(savingAutoManage = true, autoManageStatus = null) }
        viewModelScope.launch {
            runCatching {
                repository.setAutoManage(
                    instanceId,
                    SetAutoManageRequest(
                        trigger = form.trigger, interval = form.interval.trim().ifBlank { "30m" },
                        chatId = form.chatId.trim().ifBlank { null }, threadId = form.threadId.trim().ifBlank { null },
                        goalTemplate = form.goalTemplate.trim().ifBlank { null },
                    ),
                )
            }.onSuccess { c ->
                _uiState.update { it.copy(savingAutoManage = false, autoManageStatus = "Saved.", autoManageForm = AutoManageForm.from(c)) }
            }.onFailure { e ->
                _uiState.update { it.copy(savingAutoManage = false, autoManageStatus = e.message ?: "Couldn't save auto-manage settings.") }
            }
        }
    }

    fun dismissError() = _uiState.update { it.copy(error = null) }
}

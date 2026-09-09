package com.botserver.mobile.ui.automation

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.botserver.mobile.data.dto.BotInstance
import com.botserver.mobile.data.dto.HookInfo
import com.botserver.mobile.ui.components.LoadingState

private val HOOK_EVENTS = listOf("PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit")
private val EFFORT_LADDER = listOf("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra") // mirrors bot/effort.py
private val AUTO_MANAGE_TRIGGERS = listOf("scheduled", "kanban_card_created", "both")

/** Android parity with the dashboard/desktop "Automation" section — hooks,
 * agent settings, and auto-manage all already had full backend routes and
 * MCP tool exposure (Claude Desktop could already read/write them) but no
 * mobile UI until now. Reached from Settings, not a bottom-nav tab — same
 * shape as ProvidersScreen (a detail/admin screen, not everyday-use). */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AutomationScreen(onBack: () -> Unit, viewModel: AutomationViewModel = hiltViewModel()) {
    val state by viewModel.uiState.collectAsState()
    LaunchedEffect(Unit) { viewModel.refresh() }

    Scaffold(
        topBar = {
            TopAppBar(
                navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") } },
                title = { Text("Automation", fontWeight = FontWeight.Bold) },
            )
        },
    ) { padding ->
        if (state.loading && state.instances.isEmpty()) {
            LoadingState(modifier = Modifier.padding(padding))
            return@Scaffold
        }
        Column(
            modifier = Modifier.padding(padding).fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            state.error?.let {
                Surface(color = MaterialTheme.colorScheme.errorContainer, shape = RoundedCornerShape(10.dp), modifier = Modifier.fillMaxWidth()) {
                    Text(it, color = MaterialTheme.colorScheme.onErrorContainer, modifier = Modifier.padding(12.dp), style = MaterialTheme.typography.bodySmall)
                }
            }
            HooksCard(state = state, viewModel = viewModel)
            AgentSettingsCard(state = state, viewModel = viewModel)
            AutoManageCard(state = state, viewModel = viewModel)
        }
    }
}

@Composable
private fun SectionCard(title: String, subtitle: String, content: @Composable ColumnScope.() -> Unit) {
    Surface(shape = RoundedCornerShape(14.dp), tonalElevation = 1.dp, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
            Text(subtitle, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f), modifier = Modifier.padding(top = 2.dp, bottom = 12.dp))
            content()
        }
    }
}

// ------------------------------------------------------------------ hooks
@Composable
private fun HooksCard(state: AutomationUiState, viewModel: AutomationViewModel) {
    SectionCard(
        title = "Hooks",
        subtitle = "Run a local command on PreToolUse/PostToolUse/SessionStart/UserPromptSubmit. A PreToolUse hook can allow/deny/ask before a dangerous tool call runs.",
    ) {
        if (state.hooks.isEmpty()) {
            Text("No hooks configured.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
        } else {
            state.hooks.forEach { hook ->
                HookRow(hook = hook, instances = state.instances, onToggle = { viewModel.setHookEnabled(hook, it) }, onRemove = { viewModel.removeHook(hook) })
                Spacer(Modifier.height(6.dp))
            }
        }

        Spacer(Modifier.height(10.dp))
        HorizontalDivider()
        Spacer(Modifier.height(10.dp))
        Text("Add a hook", style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(8.dp))

        LabeledDropdown("Event", HOOK_EVENTS, state.newHook.event) { viewModel.updateNewHook { f -> f.copy(event = it) } }
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = state.newHook.matcher, onValueChange = { viewModel.updateNewHook { f -> f.copy(matcher = it) } },
            label = { Text("Matcher (optional), e.g. a tool name") }, modifier = Modifier.fillMaxWidth(), singleLine = true,
        )
        Spacer(Modifier.height(8.dp))
        InstanceDropdown(
            label = "Scope", instances = state.instances, selected = state.newHook.instanceId,
            noneLabel = "Global (every instance)", onSelect = { viewModel.updateNewHook { f -> f.copy(instanceId = it) } },
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = state.newHook.command, onValueChange = { viewModel.updateNewHook { f -> f.copy(command = it) } },
            label = { Text("Local shell command to run") }, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(10.dp))
        Button(onClick = viewModel::addHook, enabled = !state.addingHook, modifier = Modifier.fillMaxWidth()) {
            if (state.addingHook) CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp) else Text("+ Add hook")
        }
        state.hookStatus?.let {
            Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.tertiary, modifier = Modifier.padding(top = 6.dp))
        }
    }
}

@Composable
private fun HookRow(hook: HookInfo, instances: List<BotInstance>, onToggle: (Boolean) -> Unit, onRemove: () -> Unit) {
    val scope = hook.instanceId?.let { id -> instances.find { it.id == id }?.name ?: "instance $id" } ?: "Global"
    Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text("${hook.event}${hook.matcher?.let { " · $it" } ?: ""}", style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold)
            Text(hook.command, style = MaterialTheme.typography.bodySmall, maxLines = 2)
            Text(scope, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
        }
        Switch(checked = hook.enabled, onCheckedChange = onToggle)
        IconButton(onClick = onRemove) {
            Icon(Icons.Filled.Delete, contentDescription = "Remove hook", tint = MaterialTheme.colorScheme.error)
        }
    }
}

// --------------------------------------------------------- agent settings
@Composable
private fun AgentSettingsCard(state: AutomationUiState, viewModel: AutomationViewModel) {
    val form = state.agentSettingsForm
    SectionCard(
        title = "Agent settings",
        subtitle = "Pick an instance to edit its own settings, or leave it on \"Process-wide default\" to edit the fallback every instance with nothing configured inherits from.",
    ) {
        InstanceDropdown(
            label = "Instance", instances = state.instances, selected = state.agentSettingsInstanceId,
            noneLabel = "Process-wide default", onSelect = viewModel::selectAgentSettingsInstance,
        )
        Spacer(Modifier.height(10.dp))
        OutlinedTextField(
            value = form.maxConcurrentChildren, onValueChange = { v -> viewModel.updateAgentSettingsForm { it.copy(maxConcurrentChildren = v) } },
            label = { Text("Max concurrent children") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = form.workerProvider, onValueChange = { v -> viewModel.updateAgentSettingsForm { it.copy(workerProvider = v) } },
            label = { Text("Worker provider") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = form.workerModel, onValueChange = { v -> viewModel.updateAgentSettingsForm { it.copy(workerModel = v) } },
            label = { Text("Worker model") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        LabeledDropdown("Worker effort", listOf("(unset)") + EFFORT_LADDER, form.workerEffort.ifBlank { "(unset)" }) { v ->
            viewModel.updateAgentSettingsForm { it.copy(workerEffort = if (v == "(unset)") "" else v) }
        }
        Spacer(Modifier.height(8.dp))
        LabeledDropdown("Manager effort", listOf("(unset)") + EFFORT_LADDER, form.managerEffort.ifBlank { "(unset)" }) { v ->
            viewModel.updateAgentSettingsForm { it.copy(managerEffort = if (v == "(unset)") "" else v) }
        }
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = form.fallbackProvider, onValueChange = { v -> viewModel.updateAgentSettingsForm { it.copy(fallbackProvider = v) } },
            label = { Text("Fallback provider") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = form.fallbackModel, onValueChange = { v -> viewModel.updateAgentSettingsForm { it.copy(fallbackModel = v) } },
            label = { Text("Fallback model") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(10.dp))
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Text("Require plan approval", modifier = Modifier.weight(1f))
            Switch(checked = form.requirePlanApproval, onCheckedChange = { v -> viewModel.updateAgentSettingsForm { it.copy(requirePlanApproval = v) } })
        }
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.weight(1f)) {
                Text("Is admin instance")
                Text(
                    "Gets the admin_* tool set (Server Chat / Telegram admin pipeline). Settable only here.",
                    style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f),
                )
            }
            Switch(checked = form.isAdminInstance, onCheckedChange = { v -> viewModel.updateAgentSettingsForm { it.copy(isAdminInstance = v) } })
        }
        Spacer(Modifier.height(10.dp))
        Button(onClick = viewModel::saveAgentSettings, enabled = !state.savingAgentSettings, modifier = Modifier.fillMaxWidth()) {
            if (state.savingAgentSettings) CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp) else Text("Save")
        }
        state.agentSettingsStatus?.let {
            Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.tertiary, modifier = Modifier.padding(top = 6.dp))
        }
    }
}

// ----------------------------------------------------------- auto-manage
@Composable
private fun AutoManageCard(state: AutomationUiState, viewModel: AutomationViewModel) {
    val form = state.autoManageForm
    SectionCard(
        title = "Auto-manage",
        subtitle = "Let a manager-persona instance autonomously check in and organize work — same tool access and approval gating as any ordinary prompt.",
    ) {
        InstanceDropdown(
            label = "Instance", instances = state.instances, selected = state.autoManageInstanceId,
            noneLabel = "Pick an instance…", onSelect = viewModel::selectAutoManageInstance,
        )
        if (state.autoManageInstanceId == null) return@SectionCard

        Spacer(Modifier.height(10.dp))
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Text("Enabled", modifier = Modifier.weight(1f), fontWeight = FontWeight.SemiBold)
            if (state.togglingAutoManage) CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
            else Switch(checked = state.autoManageEnabled, onCheckedChange = viewModel::setAutoManageEnabled)
        }
        Spacer(Modifier.height(10.dp))
        LabeledDropdown("Trigger", AUTO_MANAGE_TRIGGERS, form.trigger) { v -> viewModel.updateAutoManageForm { it.copy(trigger = v) } }
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = form.interval, onValueChange = { v -> viewModel.updateAutoManageForm { it.copy(interval = v) } },
            label = { Text("Interval (when scheduled), e.g. 30m") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = form.chatId, onValueChange = { v -> viewModel.updateAutoManageForm { it.copy(chatId = v) } },
            label = { Text("Chat id (where check-ins go)") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = form.threadId, onValueChange = { v -> viewModel.updateAutoManageForm { it.copy(threadId = v) } },
            label = { Text("Thread id (optional)") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = form.goalTemplate, onValueChange = { v -> viewModel.updateAutoManageForm { it.copy(goalTemplate = v) } },
            label = { Text("Goal template (optional)") }, minLines = 2, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(10.dp))
        Button(onClick = viewModel::saveAutoManageSettings, enabled = !state.savingAutoManage, modifier = Modifier.fillMaxWidth()) {
            if (state.savingAutoManage) CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp) else Text("Save settings")
        }
        state.autoManageStatus?.let {
            Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.tertiary, modifier = Modifier.padding(top = 6.dp))
        }
    }
}

// ------------------------------------------------------------- shared ---
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun LabeledDropdown(label: String, options: List<String>, selected: String, onSelect: (String) -> Unit) {
    var expanded by rememberSaveable { mutableStateOf(false) }
    ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
        OutlinedTextField(
            value = selected, onValueChange = {}, readOnly = true, label = { Text(label) },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            modifier = Modifier.fillMaxWidth().menuAnchor(),
        )
        ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            options.forEach { option ->
                DropdownMenuItem(text = { Text(option) }, onClick = { onSelect(option); expanded = false })
            }
        }
    }
}

/** [selected] == null renders as [noneLabel] (either "process-wide
 * default", "global", or "pick one" depending on the card) — every
 * instance option below it is a real bot_instances.id. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun InstanceDropdown(label: String, instances: List<BotInstance>, selected: Int?, noneLabel: String, onSelect: (Int?) -> Unit) {
    var expanded by rememberSaveable { mutableStateOf(false) }
    val selectedLabel = instances.find { it.id == selected }?.name ?: noneLabel
    ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
        OutlinedTextField(
            value = selectedLabel, onValueChange = {}, readOnly = true, label = { Text(label) },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            modifier = Modifier.fillMaxWidth().menuAnchor(),
        )
        ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            DropdownMenuItem(text = { Text(noneLabel) }, onClick = { onSelect(null); expanded = false })
            instances.forEach { inst ->
                DropdownMenuItem(text = { Text(inst.name) }, onClick = { onSelect(inst.id); expanded = false })
            }
        }
    }
}

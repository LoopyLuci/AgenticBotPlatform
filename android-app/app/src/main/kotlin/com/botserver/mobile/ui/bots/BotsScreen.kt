package com.botserver.mobile.ui.bots

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.botserver.mobile.data.dto.BotInstance
import com.botserver.mobile.data.dto.PairingRequest
import com.botserver.mobile.data.dto.PersonaPreset
import com.botserver.mobile.ui.components.EmptyState
import com.botserver.mobile.ui.components.ErrorState
import com.botserver.mobile.ui.components.LoadingState
import com.botserver.mobile.security.rememberFragmentActivity
import com.botserver.mobile.security.requireBiometricAuth
import kotlinx.coroutines.launch

private val PLATFORMS = listOf("telegram", "discord", "slack")
private val BACKENDS = listOf("cli", "api", "ui", "hermes_cli", "hermes_gateway")

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun BotsScreen(viewModel: BotsViewModel = hiltViewModel()) {
    val state by viewModel.uiState.collectAsState()
    LaunchedEffect(Unit) { viewModel.refresh() }
    BackHandler(enabled = state.form != null) { viewModel.cancelForm() }

    val activity = rememberFragmentActivity()
    val scope = rememberCoroutineScope()
    // Gates a sensitive action behind the device's own lock-screen check —
    // see security/BiometricGate.kt. Falls through ungated if this
    // composable somehow isn't hosted in a FragmentActivity.
    fun gated(title: String, action: () -> Unit) {
        val host = activity
        if (host == null) { action(); return }
        scope.launch { if (requireBiometricAuth(host, title)) action() }
    }

    if (state.form != null) {
        // Viewing an existing bot's form is already gated at the onEdit
        // call site below (once, on open) — creating a new bot has no
        // prior secret to expose, so onSave itself isn't gated again here.
        BotFormScreen(
            form = state.form!!,
            personas = state.personas,
            onChange = viewModel::updateForm,
            onCancel = viewModel::cancelForm,
            onSave = viewModel::saveForm,
        )
        return
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Bots", fontWeight = FontWeight.Bold) },
                actions = {
                    IconButton(onClick = { viewModel.startCreate() }) {
                        Icon(Icons.Filled.Add, contentDescription = "Add bot")
                    }
                },
            )
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            if (state.loading && state.bots.isEmpty() && state.pendingPairings.isEmpty()) {
                LoadingState()
            } else if (state.error != null && state.bots.isEmpty() && state.pendingPairings.isEmpty()) {
                ErrorState(state.error!!, onRetry = { viewModel.refresh() })
            } else if (state.bots.isEmpty() && state.pendingPairings.isEmpty()) {
                EmptyState("No bots configured yet — tap + to add one.")
            } else {
                LazyColumn(contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (state.pendingPairings.isNotEmpty()) {
                        item {
                            Text(
                                "Pending pairings",
                                style = MaterialTheme.typography.labelLarge,
                                modifier = Modifier.padding(bottom = 4.dp),
                            )
                        }
                        items(state.pendingPairings, key = { "pairing-${it.id}" }) { request ->
                            PairingRow(
                                request,
                                botName = state.bots.find { it.id == request.instanceId }?.name ?: "#${request.instanceId}",
                                busy = state.busyPairingId == request.id,
                                onApprove = { gated("Confirm it's you to approve this device") { viewModel.approvePairing(request) } },
                                onDeny = { viewModel.denyPairing(request) },
                            )
                        }
                        if (state.bots.isNotEmpty()) {
                            item { Spacer(Modifier.height(4.dp)) }
                        }
                    }
                    items(state.bots, key = { it.id }) { bot ->
                        BotRow(
                            bot,
                            personaIcon = state.personas.find { it.id == bot.persona }?.icon ?: "💬",
                            busy = state.busyId == bot.id,
                            onEdit = { gated("Confirm it's you to view \"${bot.name}\"'s credentials") { viewModel.startEdit(bot) } },
                            onToggleEnabled = { viewModel.toggleEnabled(bot) },
                            onToggleRunning = { viewModel.toggleRunning(bot) },
                            onRestart = { viewModel.restart(bot) },
                            onDelete = { gated("Confirm it's you to delete \"${bot.name}\"") { viewModel.delete(bot) } },
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun BotRow(
    bot: BotInstance,
    personaIcon: String,
    busy: Boolean,
    onEdit: () -> Unit,
    onToggleEnabled: () -> Unit,
    onToggleRunning: () -> Unit,
    onRestart: () -> Unit,
    onDelete: () -> Unit,
) {
    var menuOpen by remember { mutableStateOf(false) }
    var confirmDelete by remember { mutableStateOf(false) }

    Surface(shape = RoundedCornerShape(14.dp), tonalElevation = 1.dp, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.weight(1f)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(personaIcon, modifier = Modifier.padding(end = 6.dp))
                        Text(bot.name, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
                    }
                    val subtitle = buildString {
                        append(bot.platform)
                        append(" · ")
                        append(bot.backend)
                        if (!bot.model.isNullOrBlank()) { append(" · "); append(bot.model) }
                    }
                    Text(subtitle, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
                }
                if (busy) {
                    CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
                } else {
                    Box {
                        IconButton(onClick = { menuOpen = true }) {
                            Icon(Icons.Filled.MoreVert, contentDescription = "More actions")
                        }
                        DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                            DropdownMenuItem(text = { Text("Edit") }, onClick = { menuOpen = false; onEdit() })
                            DropdownMenuItem(text = { Text("Restart") }, onClick = { menuOpen = false; onRestart() })
                            DropdownMenuItem(
                                text = { Text("Delete", color = MaterialTheme.colorScheme.error) },
                                onClick = { menuOpen = false; confirmDelete = true },
                            )
                        }
                    }
                }
            }
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                AssistChip(onClick = onToggleEnabled, enabled = !busy, label = { Text(if (bot.enabled) "Enabled" else "Disabled") })
                if (bot.enabled) {
                    AssistChip(onClick = onToggleRunning, enabled = !busy, label = { Text(if (bot.liveRunning) "Running" else "Stopped") })
                }
            }
            bot.lastError?.let {
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(top = 6.dp))
            }
        }
    }

    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text("Delete ${bot.name}?") },
            text = { Text("This permanently removes the bot instance and stops it if running. This can't be undone.") },
            confirmButton = {
                TextButton(onClick = { confirmDelete = false; onDelete() }) {
                    Text("Delete", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun PairingRow(
    request: PairingRequest,
    botName: String,
    busy: Boolean,
    onApprove: () -> Unit,
    onDeny: () -> Unit,
) {
    Surface(shape = RoundedCornerShape(14.dp), tonalElevation = 1.dp, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Text("${request.userName ?: request.userId} → $botName", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
            Text(
                "Code ${request.code} · requested ${request.createdAt}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f),
            )
            Spacer(Modifier.height(8.dp))
            if (busy) {
                CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
            } else {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = onApprove) { Text("Approve") }
                    OutlinedButton(onClick = onDeny) { Text("Deny") }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun BotFormScreen(
    form: BotForm,
    personas: List<PersonaPreset>,
    onChange: ((BotForm) -> BotForm) -> Unit,
    onCancel: () -> Unit,
    onSave: () -> Unit,
) {
    Scaffold(
        topBar = {
            TopAppBar(
                navigationIcon = {
                    IconButton(onClick = onCancel) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Cancel") }
                },
                title = { Text(if (form.isEditing) "Edit bot" else "Add bot") },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
        ) {
            OutlinedTextField(
                value = form.name,
                onValueChange = { v -> onChange { it.copy(name = v) } },
                label = { Text("Name") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
            )
            Spacer(Modifier.height(14.dp))
            LabeledDropdown("Platform", PLATFORMS, form.platform) { v -> onChange { it.copy(platform = v) } }
            Spacer(Modifier.height(14.dp))
            LabeledDropdown("Backend", BACKENDS, form.backend) { v -> onChange { it.copy(backend = v) } }
            Spacer(Modifier.height(14.dp))
            OutlinedTextField(
                value = form.model,
                onValueChange = { v -> onChange { it.copy(model = v) } },
                label = { Text("Model override (optional)") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                supportingText = { Text("Leave blank to use the backend's configured default.") },
            )
            Spacer(Modifier.height(14.dp))
            Text("Persona", style = MaterialTheme.typography.labelLarge)
            Spacer(Modifier.height(6.dp))
            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
            ) {
                personas.forEach { p ->
                    FilterChip(
                        selected = form.persona == p.id,
                        onClick = { onChange { it.copy(persona = p.id) } },
                        label = { Text("${p.icon} ${p.label}") },
                    )
                }
            }
            personas.find { it.id == form.persona }?.let {
                Spacer(Modifier.height(4.dp))
                Text(it.description, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
            }
            Spacer(Modifier.height(14.dp))
            if (form.loadingCredentials) {
                LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(6.dp))
                Text("Loading this bot's saved credentials…", style = MaterialTheme.typography.bodySmall)
            } else {
                SecretTextField(
                    value = form.botToken,
                    onValueChange = { v -> onChange { it.copy(botToken = v) } },
                    label = "Bot token",
                )
                if (form.platform == "slack") {
                    Spacer(Modifier.height(14.dp))
                    SecretTextField(
                        value = form.appToken,
                        onValueChange = { v -> onChange { it.copy(appToken = v) } },
                        label = "App token (xapp-...)",
                    )
                }
            }
            Spacer(Modifier.height(14.dp))
            OutlinedTextField(
                value = form.allowedIds,
                onValueChange = { v -> onChange { it.copy(allowedIds = v) } },
                label = { Text("Allowed user ID(s)") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                supportingText = { Text("Comma-separated. Numeric IDs for Telegram/Discord, member IDs (U.../W...) for Slack.") },
            )
            if (form.isEditing) {
                Spacer(Modifier.height(14.dp))
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                    Text("Enabled", modifier = Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
                    Switch(checked = form.enabled, onCheckedChange = { v -> onChange { it.copy(enabled = v) } })
                }
            }
            form.error?.let {
                Spacer(Modifier.height(10.dp))
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }
            Spacer(Modifier.height(20.dp))
            Button(onClick = onSave, enabled = !form.saving && !form.loadingCredentials, modifier = Modifier.fillMaxWidth()) {
                if (form.saving) CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp, color = Color.White)
                else Text(if (form.isEditing) "Save changes" else "Add bot")
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

@Composable
private fun SecretTextField(value: String, onValueChange: (String) -> Unit, label: String) {
    var visible by rememberSaveable { mutableStateOf(false) }
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text(label) },
        modifier = Modifier.fillMaxWidth(),
        singleLine = true,
        visualTransformation = if (visible) VisualTransformation.None else PasswordVisualTransformation(),
        trailingIcon = {
            IconButton(onClick = { visible = !visible }) {
                Icon(
                    if (visible) Icons.Filled.VisibilityOff else Icons.Filled.Visibility,
                    contentDescription = if (visible) "Hide $label" else "Show $label",
                )
            }
        },
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun LabeledDropdown(label: String, options: List<String>, selected: String, onSelect: (String) -> Unit) {
    var expanded by rememberSaveable { mutableStateOf(false) }
    ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
        OutlinedTextField(
            value = selected,
            onValueChange = {},
            readOnly = true,
            label = { Text(label) },
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

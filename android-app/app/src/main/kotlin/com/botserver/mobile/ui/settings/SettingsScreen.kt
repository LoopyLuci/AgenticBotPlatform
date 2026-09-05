package com.botserver.mobile.ui.settings

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel

/** Mirrors the desktop dashboard's Control Center card-for-card: backend
 * router, models, agent control, feature toggles — same settings, same
 * underlying config file, just a phone-shaped layout. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    onOpenProviders: () -> Unit = {},
    onUnpaired: () -> Unit = {},
    viewModel: SettingsViewModel = hiltViewModel(),
) {
    val state by viewModel.uiState.collectAsState()
    var showForgetDialog by rememberSaveable { mutableStateOf(false) }
    LaunchedEffect(Unit) { viewModel.refresh() }

    Scaffold(topBar = { TopAppBar(title = { Text("Settings", fontWeight = FontWeight.Bold) }) }) { padding ->
        if (state.loading) {
            Box(Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
            return@Scaffold
        }
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
        ) {
            state.error?.let {
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(bottom = 12.dp))
            }

            SettingsCard(title = "Backend router — Claude") {
                SettingsRow("Default backend", "Used when no action-type override matches") {
                    SegmentedPicker(
                        options = listOf("ui", "cli", "api"),
                        selected = state.defaultBackend,
                        busy = state.savingKey == "default_backend",
                        onSelect = viewModel::setDefaultBackend,
                    )
                }
            }

            Spacer(Modifier.height(14.dp))

            SettingsCard(title = "Backend router — Hermes Agent") {
                SettingsRow("Default backend", "Its own slot, separate from Claude's — picking one here never changes Claude's default") {
                    SegmentedPicker(
                        options = listOf("hermes_cli", "hermes_gateway"),
                        selected = state.defaultHermesBackend,
                        busy = state.savingKey == "default_hermes_backend",
                        onSelect = viewModel::setDefaultHermesBackend,
                    )
                }
            }

            Spacer(Modifier.height(14.dp))

            SettingsCard(title = "Models — Claude") {
                SettingsRow("api (Anthropic API)", "Model used for every request routed to the api backend") {
                    ModelField(
                        value = state.apiModel,
                        knownOptions = state.knownApiModels,
                        busy = state.savingKey == "model_api",
                        onCommit = viewModel::setApiModel,
                    )
                }
            }

            Spacer(Modifier.height(14.dp))

            SettingsCard(title = "Models — Hermes Agent") {
                SettingsRow("hermes_cli", "Blank uses Hermes's own default") {
                    ModelField(value = state.hermesCliModel, knownOptions = emptyList(), busy = state.savingKey == "model_hermes_cli", onCommit = viewModel::setHermesCliModel)
                }
                Spacer(Modifier.height(10.dp))
                SettingsRow("hermes_gateway", "Blank uses Hermes's own default") {
                    ModelField(value = state.hermesGatewayModel, knownOptions = emptyList(), busy = state.savingKey == "model_hermes_gateway", onCommit = viewModel::setHermesGatewayModel)
                }
            }

            Spacer(Modifier.height(14.dp))

            Surface(
                shape = RoundedCornerShape(16.dp),
                tonalElevation = 1.dp,
                onClick = onOpenProviders,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.fillMaxWidth().padding(16.dp),
                ) {
                    Column(Modifier.weight(1f)) {
                        Text("Providers & Models", style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold)
                        Text(
                            "Custom OpenAI-compatible endpoints for custom_model/native_agent — add a key, browse and toggle models",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f),
                        )
                    }
                    Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, contentDescription = null)
                }
            }

            Spacer(Modifier.height(14.dp))

            SettingsCard(title = "Agent control") {
                SettingsRow("Cross-agent targeting", "Whether one bot instance may ask/command another") {
                    SegmentedPicker(
                        options = listOf("trust_all", "allowlist"),
                        selected = state.agentControlMode,
                        busy = state.savingKey == "agent_control",
                        onSelect = viewModel::setAgentControlMode,
                    )
                }
            }

            Spacer(Modifier.height(14.dp))

            SettingsCard(title = "Feature toggles") {
                ToggleRow("UI automation backend", "Allow routing to the Desktop window", state.uiAutomationEnabled, state.savingKey == "ui_automation", viewModel::setUiAutomationEnabled)
                Spacer(Modifier.height(4.dp))
                ToggleRow("Confirm destructive actions", "Restart / stop / MCP changes need a tap", state.confirmDestructive, state.savingKey == "confirm_destructive", viewModel::setConfirmDestructive)
                Spacer(Modifier.height(4.dp))
                ToggleRow("Verbose telemetry", "Log every backend call, not just failures", state.verboseTelemetry, state.savingKey == "verbose_telemetry", viewModel::setVerboseTelemetry)
            }

            Spacer(Modifier.height(14.dp))

            SettingsCard(title = "Danger zone") {
                Text(
                    "Clears this phone's stored server address and key, and takes you back to the pairing screen. Your pairing key itself stays valid until you revoke it from the desktop/web dashboard's Mobile tab.",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f),
                )
                Spacer(Modifier.height(10.dp))
                OutlinedButton(
                    onClick = { showForgetDialog = true },
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = MaterialTheme.colorScheme.error),
                    modifier = Modifier.fillMaxWidth().testTag("settings-forget-pairing"),
                ) {
                    Text("Clear app settings (forget this server)")
                }
            }

            Spacer(Modifier.height(24.dp))
        }
    }

    if (showForgetDialog) {
        AlertDialog(
            onDismissRequest = { showForgetDialog = false },
            title = { Text("Clear app settings?") },
            text = { Text("This phone will forget the paired server and key, and return to the pairing screen. You'll need to scan a QR or enter a key again to reconnect.") },
            confirmButton = {
                TextButton(
                    onClick = {
                        showForgetDialog = false
                        viewModel.forgetPairing()
                        onUnpaired()
                    },
                    modifier = Modifier.testTag("settings-forget-pairing-confirm"),
                ) { Text("Clear settings", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { showForgetDialog = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun SettingsCard(title: String, content: @Composable ColumnScope.() -> Unit) {
    Surface(shape = RoundedCornerShape(16.dp), tonalElevation = 1.dp, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Text(title.uppercase(), style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f), fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(10.dp))
            content()
        }
    }
}

@Composable
private fun SettingsRow(title: String, description: String, control: @Composable () -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.weight(1f).padding(end = 10.dp)) {
            Text(title, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold)
            Text(description, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
        }
        control()
    }
}

@Composable
private fun ToggleRow(title: String, description: String, checked: Boolean, busy: Boolean, onChange: (Boolean) -> Unit) {
    SettingsRow(title, description) {
        if (busy) CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
        else Switch(checked = checked, onCheckedChange = onChange)
    }
}

@Composable
private fun SegmentedPicker(options: List<String>, selected: String, busy: Boolean, onSelect: (String) -> Unit) {
    if (busy) {
        CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
        return
    }
    Row(
        modifier = Modifier
            .clip(RoundedCornerShape(999.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .padding(3.dp),
    ) {
        options.forEach { option ->
            val active = option == selected
            Surface(
                shape = RoundedCornerShape(999.dp),
                color = if (active) MaterialTheme.colorScheme.primary else Color.Transparent,
                onClick = { if (!active) onSelect(option) },
            ) {
                Text(
                    option,
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
                    style = MaterialTheme.typography.labelMedium,
                    color = if (active) Color.White else MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ModelField(value: String, knownOptions: List<String>, busy: Boolean, onCommit: (String) -> Unit) {
    var text by rememberSaveable(value) { mutableStateOf(value) }
    var expanded by rememberSaveable { mutableStateOf(false) }

    if (knownOptions.isNotEmpty()) {
        ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }, modifier = Modifier.width(190.dp)) {
            OutlinedTextField(
                value = text.ifBlank { "(default)" },
                onValueChange = {},
                readOnly = true,
                enabled = !busy,
                trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
                textStyle = MaterialTheme.typography.bodySmall,
                modifier = Modifier.menuAnchor(),
            )
            ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
                knownOptions.forEach { option ->
                    DropdownMenuItem(text = { Text(option) }, onClick = { text = option; expanded = false; onCommit(option) })
                }
            }
        }
    } else {
        OutlinedTextField(
            value = text,
            onValueChange = { text = it },
            enabled = !busy,
            placeholder = { Text("(default)") },
            textStyle = MaterialTheme.typography.bodySmall,
            singleLine = true,
            modifier = Modifier.width(190.dp),
            keyboardActions = KeyboardActions(onDone = { onCommit(text) }),
            trailingIcon = {
                if (busy) CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
            },
        )
    }
}

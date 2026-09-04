package com.botserver.mobile.ui.providers

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.ExpandLess
import androidx.compose.material.icons.filled.ExpandMore
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
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.botserver.mobile.data.dto.BrowseModel
import com.botserver.mobile.data.dto.CatalogProvider
import com.botserver.mobile.data.dto.ProviderInfo
import com.botserver.mobile.ui.components.EmptyState
import com.botserver.mobile.ui.components.ErrorState
import com.botserver.mobile.ui.components.LoadingState
import com.botserver.mobile.ui.components.PullRefreshBox

/** Full parity with the desktop dashboard's Providers + Models page — add
 * a named OpenAI-compatible endpoint (catalog-assisted or fully custom),
 * expand it to browse its models (free-first), and toggle individual
 * models or every non-free one at once. Reached from Settings (see
 * SettingsScreen's "Providers & Models" row), not a bottom-nav tab — this
 * is a detail screen, same shape as a bot's own edit form. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ProvidersScreen(onBack: () -> Unit, viewModel: ProvidersViewModel = hiltViewModel()) {
    val state by viewModel.uiState.collectAsState()
    LaunchedEffect(Unit) { viewModel.refresh() }
    BackHandler(enabled = state.showAddForm) { viewModel.cancelAdd() }

    if (state.showAddForm) {
        AddProviderScreen(
            form = state.addForm,
            catalog = state.catalog,
            onChange = viewModel::updateAddForm,
            onSelectCatalog = viewModel::selectCatalogEntry,
            onCancel = viewModel::cancelAdd,
            onSave = viewModel::saveAddForm,
        )
        return
    }

    Scaffold(
        topBar = {
            TopAppBar(
                navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") } },
                title = { Text("Providers & Models", fontWeight = FontWeight.Bold) },
                actions = {
                    IconButton(onClick = { viewModel.startAdd() }, modifier = Modifier.testTag("providers-add")) {
                        Icon(Icons.Filled.Add, contentDescription = "Add provider")
                    }
                },
            )
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            when {
                state.loading && state.providers.isEmpty() -> LoadingState()
                state.error != null && state.providers.isEmpty() -> ErrorState(state.error!!, onRetry = { viewModel.refresh() })
                state.providers.isEmpty() -> EmptyState(
                    "No providers configured yet — tap + to add one (a local Ollama/LM Studio server, or a real cloud provider like OpenRouter).",
                )
                else -> PullRefreshBox(refreshing = state.loading, onRefresh = { viewModel.refresh() }, modifier = Modifier.fillMaxSize()) {
                    LazyColumn(
                        modifier = Modifier.testTag("providers-list"),
                        contentPadding = PaddingValues(12.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        state.error?.let { message ->
                            item(key = "error") {
                                Text(message, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                            }
                        }
                        state.providers.forEach { provider ->
                            item(key = "header-${provider.name}") {
                                ProviderHeaderRow(
                                    provider = provider,
                                    expanded = state.expanded.contains(provider.name),
                                    modelCount = state.modelsByProvider[provider.name]?.size,
                                    busy = state.busyProvider == provider.name,
                                    onToggleExpanded = { viewModel.toggleExpanded(provider.name) },
                                    onDelete = { viewModel.deleteProvider(provider.name) },
                                )
                            }
                            if (state.expanded.contains(provider.name)) {
                                val models = state.modelsByProvider[provider.name]
                                if (models == null) {
                                    item(key = "loading-${provider.name}") {
                                        LinearProgressIndicator(modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp))
                                    }
                                } else if (models.isEmpty()) {
                                    item(key = "empty-${provider.name}") {
                                        Text(
                                            "No models found yet — add a real API key, or this provider isn't in the known catalog and hasn't responded live.",
                                            style = MaterialTheme.typography.bodySmall,
                                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f),
                                            modifier = Modifier.padding(start = 8.dp),
                                        )
                                    }
                                } else {
                                    item(key = "bulk-${provider.name}") {
                                        ProviderBulkActions(
                                            models = models,
                                            busy = state.busyProvider == provider.name,
                                            onTurnOffPaid = { viewModel.toggleAllPaid(provider.name, false) },
                                            onTurnOnPaid = { viewModel.toggleAllPaid(provider.name, true) },
                                        )
                                    }
                                    items(models, key = { "model-${provider.name}-${it.id}" }) { model ->
                                        ModelRow(model = model, onToggle = { viewModel.toggleModel(provider.name, model) })
                                    }
                                }
                            }
                        }
                        item(key = "bottom-spacer") { Spacer(Modifier.height(12.dp)) }
                    }
                }
            }
        }
    }
}

@Composable
private fun ProviderHeaderRow(
    provider: ProviderInfo,
    expanded: Boolean,
    modelCount: Int?,
    busy: Boolean,
    onToggleExpanded: () -> Unit,
    onDelete: () -> Unit,
) {
    var confirmDelete by remember { mutableStateOf(false) }

    Surface(shape = RoundedCornerShape(14.dp), tonalElevation = 1.dp, onClick = onToggleExpanded, modifier = Modifier.fillMaxWidth()) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 10.dp),
        ) {
            Column(Modifier.weight(1f)) {
                Text(provider.name, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
                val subtitle = buildString {
                    append(provider.baseUrl ?: "")
                    if (modelCount != null) { append(" · "); append(modelCount); append(if (modelCount == 1) " model" else " models") }
                }
                Text(subtitle, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
            }
            if (busy) {
                CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
            } else {
                IconButton(onClick = { confirmDelete = true }) {
                    Icon(Icons.Filled.Delete, contentDescription = "Delete ${provider.name}", tint = MaterialTheme.colorScheme.error)
                }
                IconButton(onClick = onToggleExpanded) {
                    Icon(
                        if (expanded) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore,
                        contentDescription = if (expanded) "Collapse" else "Expand",
                    )
                }
            }
        }
    }

    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text("Delete ${provider.name}?") },
            text = { Text("Removes this provider from config/providers.yaml. Any bot instance still pointed at it will fail until reassigned.") },
            confirmButton = {
                TextButton(onClick = { confirmDelete = false; onDelete() }) { Text("Delete", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun ProviderBulkActions(
    models: List<BrowseModel>,
    busy: Boolean,
    onTurnOffPaid: () -> Unit,
    onTurnOnPaid: () -> Unit,
) {
    val paidTotal = models.count { it.free != true }
    val paidHidden = models.count { it.free != true && !it.enabled }
    Column(Modifier.padding(horizontal = 4.dp)) {
        Row(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.horizontalScroll(rememberScrollState()),
        ) {
            OutlinedButton(onClick = onTurnOffPaid, enabled = !busy) { Text("Turn Off All Paid") }
            OutlinedButton(onClick = onTurnOnPaid, enabled = !busy) { Text("Turn On All Paid Models") }
        }
        if (paidTotal > 0) {
            Spacer(Modifier.height(4.dp))
            Text(
                "$paidHidden of $paidTotal paid/unpriced model${if (paidTotal == 1) "" else "s"} hidden by default",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f),
            )
        }
    }
}

@Composable
private fun ModelRow(model: BrowseModel, onToggle: () -> Unit) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 6.dp),
    ) {
        Column(Modifier.weight(1f).padding(end = 10.dp)) {
            Text(model.id, style = MaterialTheme.typography.bodySmall, fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace)
            Row(verticalAlignment = Alignment.CenterVertically) {
                FreeBadge(model.free)
                val price = if (model.input != null && model.output != null) {
                    "  $${"%.2f".format(model.input * 1e6)}/${"%.2f".format(model.output * 1e6)} per 1M in/out"
                } else null
                price?.let { Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f)) }
            }
        }
        Switch(checked = model.enabled, onCheckedChange = { onToggle() })
    }
}

@Composable
private fun FreeBadge(free: Boolean?) {
    val (label, color) = when (free) {
        true -> "free" to MaterialTheme.colorScheme.primary
        false -> "paid" to MaterialTheme.colorScheme.error
        null -> "—" to MaterialTheme.colorScheme.onSurface.copy(alpha = 0.5f)
    }
    Text(label, style = MaterialTheme.typography.labelSmall, color = color, fontWeight = FontWeight.SemiBold)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AddProviderScreen(
    form: AddProviderForm,
    catalog: List<CatalogProvider>,
    onChange: ((AddProviderForm) -> AddProviderForm) -> Unit,
    onSelectCatalog: (CatalogProvider) -> Unit,
    onCancel: () -> Unit,
    onSave: () -> Unit,
) {
    var catalogMenuOpen by remember { mutableStateOf(false) }

    Scaffold(
        topBar = {
            TopAppBar(
                navigationIcon = { IconButton(onClick = onCancel) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Cancel") } },
                title = { Text("Add provider") },
            )
        },
        // The Save button lives in bottomBar — see PairingScreen.kt's
        // bottomBar for the same reasoning: always visible without
        // scrolling, with real navigation-bar inset handling.
        bottomBar = {
            Surface(tonalElevation = 2.dp) {
                Button(
                    onClick = onSave,
                    enabled = !form.saving,
                    modifier = Modifier
                        .fillMaxWidth()
                        .navigationBarsPadding()
                        .padding(16.dp)
                        .testTag("provider-form-save"),
                ) {
                    if (form.saving) CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp, color = Color.White)
                    else Text("Add provider")
                }
            }
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
        ) {
            if (catalog.isNotEmpty()) {
                Box {
                    OutlinedButton(onClick = { catalogMenuOpen = true }, modifier = Modifier.fillMaxWidth()) {
                        Text("Pick a known provider (optional)…")
                    }
                    DropdownMenu(expanded = catalogMenuOpen, onDismissRequest = { catalogMenuOpen = false }) {
                        catalog.forEach { entry ->
                            DropdownMenuItem(
                                text = { Text("${entry.name} (${entry.id})") },
                                onClick = { catalogMenuOpen = false; onSelectCatalog(entry) },
                            )
                        }
                    }
                }
                Spacer(Modifier.height(14.dp))
            }
            OutlinedTextField(
                value = form.name,
                onValueChange = { v -> onChange { it.copy(name = v, catalogId = if (v != it.name) null else it.catalogId) } },
                label = { Text("Name") },
                modifier = Modifier.fillMaxWidth().testTag("provider-form-name"),
                singleLine = true,
                supportingText = { Text("Used as the \"<name>/<model_id>\" prefix — may not contain '/'.") },
            )
            Spacer(Modifier.height(14.dp))
            OutlinedTextField(
                value = form.baseUrl,
                onValueChange = { v -> onChange { it.copy(baseUrl = v) } },
                label = { Text("Base URL") },
                modifier = Modifier.fillMaxWidth().testTag("provider-form-base-url"),
                singleLine = true,
                placeholder = { Text("https://openrouter.ai/api/v1") },
            )
            Spacer(Modifier.height(14.dp))
            OutlinedTextField(
                value = form.apiKeyEnv,
                onValueChange = { v -> onChange { it.copy(apiKeyEnv = v) } },
                label = { Text("API key env var (optional)") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                supportingText = { Text("Preferred over the field below when both are set — reads a real env var at request time.") },
            )
            Spacer(Modifier.height(14.dp))
            SecretTextField(
                value = form.apiKey,
                onValueChange = { v -> onChange { it.copy(apiKey = v) } },
                label = "Inline API key (optional)",
                modifier = Modifier.testTag("provider-form-api-key"),
            )
            form.error?.let {
                Spacer(Modifier.height(10.dp))
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }
            Spacer(Modifier.height(20.dp))
        }
    }
}

@Composable
private fun SecretTextField(value: String, onValueChange: (String) -> Unit, label: String, modifier: Modifier = Modifier) {
    var visible by rememberSaveable { mutableStateOf(false) }
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text(label) },
        modifier = modifier.fillMaxWidth(),
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

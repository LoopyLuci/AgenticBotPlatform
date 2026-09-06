package com.botserver.mobile.ui.model

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog

/** Free-tier model ids follow a "...-free"/"...:free" suffix convention
 * across every provider BotServer's own catalogs use — mirrors
 * bot.commands.is_free_model_id() exactly, so the 🆓 badge here always
 * agrees with what the server itself considers free. */
private val FREE_MODEL_SUFFIX = Regex("[-:_]free$", RegexOption.IGNORE_CASE)
private fun isFreeModelId(modelId: String): Boolean = FREE_MODEL_SUFFIX.containsMatchIn(modelId)

/** The native equivalent of Telegram's interactive /model inline
 * keyboard (see bot/handlers.py's _model_providers_page/_model_page) —
 * a two-level picker (provider list, then a paginated model list within
 * one provider) backed by the same GET /api/bots/{id}/model-picker data.
 * Rendered whenever ModelPickerViewModel.state.visible is true; the
 * caller (ChatScreen) owns opening it via viewModel.open(instanceId). */
@Composable
fun ModelPickerDialog(viewModel: ModelPickerViewModel) {
    val state by viewModel.state.collectAsState()
    if (!state.visible) return

    Dialog(onDismissRequest = viewModel::dismiss) {
        Surface(shape = RoundedCornerShape(16.dp), tonalElevation = 4.dp) {
            Column(modifier = Modifier.padding(20.dp).heightIn(max = 560.dp)) {
                val providerSuffix = state.providerName?.let { " — $it" } ?: ""
                Text(
                    "Model for this bot (${state.backend}$providerSuffix)",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                )
                Spacer(Modifier.height(4.dp))
                Text(
                    "Current: ${state.currentModel ?: "(backend default)"}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
                )
                Spacer(Modifier.height(12.dp))

                when {
                    state.loading || state.applying -> Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator(modifier = Modifier.size(28.dp))
                    }
                    state.error != null -> Text(
                        state.error ?: "",
                        color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.testTag("model-picker-error"),
                    )
                    state.mode == "providers" -> ProviderList(state, viewModel)
                    !state.hasKnownList -> Text(
                        "This bot's backend (${state.backend}) has no discoverable model list.",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    else -> ModelList(state, viewModel)
                }

                Spacer(Modifier.height(16.dp))
                TextButton(onClick = viewModel::dismiss, modifier = Modifier.testTag("model-picker-cancel")) {
                    Text("Cancel")
                }
            }
        }
    }
}

@Composable
private fun ProviderList(state: ModelPickerUiState, viewModel: ModelPickerViewModel) {
    Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
        state.providers.forEach { p ->
            val mark = if (p.isCurrent) "✓ " else ""
            val free = if (p.freeCount > 0) ", ${p.freeCount} free" else ""
            OutlinedButton(
                onClick = { viewModel.selectProvider(p.idx) },
                modifier = Modifier.fillMaxWidth().padding(vertical = 3.dp).testTag("model-picker-provider-${p.idx}"),
            ) {
                Text("$mark${p.name} (${p.count}$free)")
            }
        }
    }
}

@Composable
private fun ModelList(state: ModelPickerUiState, viewModel: ModelPickerViewModel) {
    Column {
        Column(modifier = Modifier.weight(1f, fill = false).verticalScroll(rememberScrollState())) {
            state.models.chunked(2).forEach { rowModels ->
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    rowModels.forEach { m ->
                        val mark = if (m == state.currentModel) "✓ " else ""
                        val free = if (isFreeModelId(m)) " 🆓" else ""
                        Button(
                            onClick = { viewModel.selectModel(m) },
                            colors = if (m == state.currentModel) ButtonDefaults.buttonColors() else ButtonDefaults.outlinedButtonColors(),
                            modifier = Modifier.weight(1f).padding(vertical = 3.dp).testTag("model-picker-model"),
                        ) {
                            Text("$mark$m$free", maxLines = 2, style = MaterialTheme.typography.labelSmall)
                        }
                    }
                    if (rowModels.size == 1) Spacer(Modifier.weight(1f))
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (state.page > 0) {
                TextButton(onClick = { viewModel.goToPage(state.page - 1) }) { Text("< Prev") }
            }
            if (state.page < state.totalPages - 1) {
                TextButton(onClick = { viewModel.goToPage(state.page + 1) }) { Text("Next >") }
            }
            if (state.multiProvider) {
                TextButton(onClick = viewModel::backToProviders) { Text("< Providers") }
            }
        }
    }
}

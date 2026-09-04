package com.botserver.mobile.ui.providers

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.ProvidersRepository
import com.botserver.mobile.data.dto.BrowseModel
import com.botserver.mobile.data.dto.CatalogProvider
import com.botserver.mobile.data.dto.ProviderInfo
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

data class AddProviderForm(
    val name: String = "",
    val baseUrl: String = "",
    val apiKeyEnv: String = "",
    val apiKey: String = "",
    val catalogId: String? = null,
    val saving: Boolean = false,
    val error: String? = null,
)

data class ProvidersUiState(
    val loading: Boolean = true,
    val error: String? = null,
    val providers: List<ProviderInfo> = emptyList(),
    val catalog: List<CatalogProvider> = emptyList(),
    val modelsByProvider: Map<String, List<BrowseModel>> = emptyMap(),
    val loadingModelsFor: Set<String> = emptySet(),
    val expanded: Set<String> = emptySet(),
    val busyProvider: String? = null,
    val showAddForm: Boolean = false,
    val addForm: AddProviderForm = AddProviderForm(),
)

/** Full parity with the desktop dashboard's Providers + Models page: add a
 * named OpenAI-compatible endpoint (catalog-assisted or fully custom),
 * browse its models (free-first, per bot.models.browse_provider_models()),
 * and toggle individual models or every paid one at once. Paid/unpriced
 * models default OFF (see bot.models._resolve_effective_enabled()) — the
 * same server-side default the dashboard/desktop UI already reflect, so
 * this screen's checkboxes and hidden-count note need no client-side
 * reimplementation of that rule, just the server's own `enabled` field. */
@HiltViewModel
class ProvidersViewModel @Inject constructor(private val repository: ProvidersRepository) : ViewModel() {

    private val _uiState = MutableStateFlow(ProvidersUiState())
    val uiState: StateFlow<ProvidersUiState> = _uiState

    fun refresh() {
        viewModelScope.launch {
            _uiState.update { it.copy(loading = it.providers.isEmpty(), error = null) }
            val providersResult = runCatching { repository.list() }
            val catalogResult = runCatching { repository.catalog() }
            val providers = providersResult.getOrNull()
            if (providers == null) {
                _uiState.update { it.copy(loading = false, error = providersResult.exceptionOrNull()?.message ?: "Couldn't load providers.") }
                return@launch
            }
            _uiState.update { it.copy(loading = false, error = null, providers = providers, catalog = catalogResult.getOrNull() ?: it.catalog) }
            // Re-fetch models for whatever's already expanded, so pull-to-
            // refresh actually updates what's currently on screen.
            _uiState.value.expanded.forEach(::loadModels)
        }
    }

    fun toggleExpanded(name: String) {
        val wasExpanded = _uiState.value.expanded.contains(name)
        _uiState.update { it.copy(expanded = if (wasExpanded) it.expanded - name else it.expanded + name) }
        if (!wasExpanded && !_uiState.value.modelsByProvider.containsKey(name)) {
            loadModels(name)
        }
    }

    private fun loadModels(name: String) {
        viewModelScope.launch {
            _uiState.update { it.copy(loadingModelsFor = it.loadingModelsFor + name) }
            val result = runCatching { repository.modelsFor(name) }
            _uiState.update {
                it.copy(
                    loadingModelsFor = it.loadingModelsFor - name,
                    modelsByProvider = result.getOrNull()?.let { models -> it.modelsByProvider + (name to models) } ?: it.modelsByProvider,
                    error = result.exceptionOrNull()?.let { e -> e.message ?: "Couldn't load $name's models." } ?: it.error,
                )
            }
        }
    }

    fun toggleModel(provider: String, model: BrowseModel) {
        val newEnabled = !model.enabled
        _uiState.update { state -> state.copy(modelsByProvider = state.modelsByProvider.withModelEnabled(provider, model.id, newEnabled)) }
        viewModelScope.launch {
            runCatching { repository.toggleModel(provider, model.id, newEnabled) }.onFailure { e ->
                _uiState.update { state ->
                    state.copy(
                        error = e.message ?: "Couldn't update that model's toggle.",
                        modelsByProvider = state.modelsByProvider.withModelEnabled(provider, model.id, !newEnabled),
                    )
                }
            }
        }
    }

    fun toggleAllPaid(provider: String, enabled: Boolean) {
        viewModelScope.launch {
            _uiState.update { it.copy(busyProvider = provider) }
            runCatching { repository.toggleAllPaidModels(provider, enabled) }
                .onFailure { e -> _uiState.update { it.copy(error = e.message ?: "Couldn't update paid models.") } }
            _uiState.update { it.copy(busyProvider = null) }
            loadModels(provider)
        }
    }

    fun deleteProvider(name: String) {
        viewModelScope.launch {
            _uiState.update { it.copy(busyProvider = name) }
            runCatching { repository.deleteProvider(name) }
                .onSuccess {
                    _uiState.update {
                        it.copy(
                            providers = it.providers.filterNot { p -> p.name == name },
                            modelsByProvider = it.modelsByProvider - name,
                            expanded = it.expanded - name,
                        )
                    }
                }
                .onFailure { e -> _uiState.update { it.copy(error = e.message ?: "Couldn't delete that provider.") } }
            _uiState.update { it.copy(busyProvider = null) }
        }
    }

    fun startAdd() = _uiState.update { it.copy(showAddForm = true, addForm = AddProviderForm()) }

    fun cancelAdd() = _uiState.update { it.copy(showAddForm = false) }

    fun updateAddForm(transform: (AddProviderForm) -> AddProviderForm) =
        _uiState.update { it.copy(addForm = transform(it.addForm)) }

    /** Selecting a known catalog provider prefills name/base_url/env — all
     * still editable/overridable, matching the desktop picker's behavior. */
    fun selectCatalogEntry(entry: CatalogProvider) {
        _uiState.update {
            it.copy(
                addForm = it.addForm.copy(
                    name = it.addForm.name.ifBlank { entry.id },
                    baseUrl = entry.api,
                    apiKeyEnv = it.addForm.apiKeyEnv.ifBlank { entry.env.firstOrNull() ?: "" },
                    catalogId = entry.id,
                ),
            )
        }
    }

    fun saveAddForm() {
        val form = _uiState.value.addForm
        if (form.name.isBlank() || form.baseUrl.isBlank()) {
            _uiState.update { it.copy(addForm = it.addForm.copy(error = "Name and base URL are both required.")) }
            return
        }
        _uiState.update { it.copy(addForm = it.addForm.copy(saving = true, error = null)) }
        viewModelScope.launch {
            runCatching {
                repository.setProvider(form.name.trim(), form.baseUrl.trim(), form.apiKeyEnv, form.apiKey, form.catalogId)
            }.onSuccess {
                _uiState.update { it.copy(showAddForm = false, addForm = AddProviderForm()) }
                refresh()
            }.onFailure { e ->
                _uiState.update { it.copy(addForm = it.addForm.copy(saving = false, error = e.message ?: "Couldn't add that provider.")) }
            }
        }
    }

    fun dismissError() = _uiState.update { it.copy(error = null) }
}

private fun Map<String, List<BrowseModel>>.withModelEnabled(provider: String, modelId: String, enabled: Boolean): Map<String, List<BrowseModel>> =
    mapValues { (p, models) ->
        if (p != provider) models else models.map { if (it.id == modelId) it.copy(enabled = enabled) else it }
    }

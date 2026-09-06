package com.botserver.mobile.ui.model

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.ApiService
import com.botserver.mobile.data.dto.ModelPickerProvider
import com.botserver.mobile.data.dto.SetModelRequest
import com.botserver.mobile.diagnostics.AppLog
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

private const val TAG = "ModelPicker"

data class ModelPickerUiState(
    val visible: Boolean = false,
    val loading: Boolean = false,
    val instanceId: Int? = null,
    val backend: String = "",
    val mode: String = "providers", // "providers" | "models", mirrors the server payload's own field
    val providers: List<ModelPickerProvider> = emptyList(),
    val providerIdx: Int? = null,
    val providerName: String? = null,
    val models: List<String> = emptyList(),
    val page: Int = 0,
    val totalPages: Int = 1,
    val multiProvider: Boolean = false,
    val hasKnownList: Boolean = true,
    val currentModel: String? = null,
    val error: String? = null,
    val applying: Boolean = false,
)

/**
 * Drives the native model-picker dialog for a bot instance's Chat screen
 * — the same two-level provider/model data Telegram's interactive
 * /model command already renders as an inline keyboard (see
 * bot/handlers.py's _model_providers_page/_model_page and the
 * GET /api/bots/{id}/model-picker route this calls), now with an
 * equivalent native Compose dialog instead of the Chat screen's plain
 * text bubble only ever showing the global per-backend-family summary.
 */
@HiltViewModel
class ModelPickerViewModel @Inject constructor(
    private val apiService: ApiService,
) : ViewModel() {

    private val _state = MutableStateFlow(ModelPickerUiState())
    val state: StateFlow<ModelPickerUiState> = _state

    fun open(instanceId: Int) {
        _state.value = ModelPickerUiState(visible = true, loading = true, instanceId = instanceId)
        load(instanceId, provider = null, page = 0)
    }

    fun dismiss() {
        _state.value = ModelPickerUiState()
    }

    fun selectProvider(idx: Int) {
        val instanceId = _state.value.instanceId ?: return
        _state.update { it.copy(loading = true, error = null) }
        load(instanceId, provider = idx, page = 0)
    }

    fun backToProviders() {
        val instanceId = _state.value.instanceId ?: return
        _state.update { it.copy(loading = true, error = null) }
        load(instanceId, provider = null, page = 0)
    }

    fun goToPage(page: Int) {
        val instanceId = _state.value.instanceId ?: return
        val providerIdx = _state.value.providerIdx
        _state.update { it.copy(loading = true, error = null) }
        load(instanceId, provider = providerIdx, page = page)
    }

    fun selectModel(model: String) {
        val instanceId = _state.value.instanceId ?: return
        _state.update { it.copy(applying = true, error = null) }
        viewModelScope.launch {
            runCatching { apiService.setInstanceModel(instanceId, SetModelRequest(model)) }
                .onSuccess {
                    AppLog.i(TAG, "set model for instance $instanceId -> $model")
                    _state.value = ModelPickerUiState()
                }
                .onFailure { e ->
                    AppLog.w(TAG, "failed to set model for instance $instanceId", e)
                    _state.update { it.copy(applying = false, error = e.message ?: "Couldn't set the model.") }
                }
        }
    }

    private fun load(instanceId: Int, provider: Int?, page: Int) {
        viewModelScope.launch {
            runCatching { apiService.modelPicker(instanceId, provider, page) }
                .onSuccess { data ->
                    _state.update {
                        it.copy(
                            loading = false,
                            instanceId = instanceId,
                            backend = data.backend,
                            mode = data.mode,
                            providers = data.providers ?: emptyList(),
                            providerIdx = data.providerIdx,
                            providerName = data.provider,
                            models = data.models ?: emptyList(),
                            page = data.page,
                            totalPages = data.totalPages,
                            multiProvider = data.multiProvider,
                            hasKnownList = data.hasKnownList,
                            currentModel = data.currentModel,
                        )
                    }
                }
                .onFailure { e ->
                    AppLog.w(TAG, "failed to load model picker for instance $instanceId", e)
                    _state.update { it.copy(loading = false, error = e.message ?: "Couldn't load models.") }
                }
        }
    }
}

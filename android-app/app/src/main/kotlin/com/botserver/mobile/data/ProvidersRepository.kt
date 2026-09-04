package com.botserver.mobile.data

import com.botserver.mobile.data.dto.BrowseModel
import com.botserver.mobile.data.dto.CatalogProvider
import com.botserver.mobile.data.dto.ModelToggleRequest
import com.botserver.mobile.data.dto.ModelTogglePaidRequest
import com.botserver.mobile.data.dto.ProviderInfo
import com.botserver.mobile.data.dto.SetProviderRequest
import javax.inject.Inject
import javax.inject.Singleton

/** Full parity with the desktop dashboard's Providers + Models page —
 * network-only (no Room), since this is the same "cache lives in the one
 * server-side config/providers.yaml + model_toggles table" shape the
 * dashboard/desktop UI already use, with no offline write path and no
 * pagination that would justify a local cache. */
@Singleton
class ProvidersRepository @Inject constructor(private val apiService: ApiService) {
    suspend fun list(): List<ProviderInfo> = apiService.providers().providers

    suspend fun catalog(): List<CatalogProvider> = apiService.providersCatalog().providers

    suspend fun modelsFor(name: String): List<BrowseModel> = apiService.providerModels(name).models

    suspend fun setProvider(
        name: String,
        baseUrl: String,
        apiKeyEnv: String?,
        apiKey: String?,
        catalogId: String?,
    ) {
        apiService.setProvider(
            SetProviderRequest(
                name = name,
                baseUrl = baseUrl,
                apiKeyEnv = apiKeyEnv?.takeIf { it.isNotBlank() },
                apiKey = apiKey?.takeIf { it.isNotBlank() },
                catalogId = catalogId?.takeIf { it.isNotBlank() },
            ),
        )
    }

    suspend fun deleteProvider(name: String) {
        apiService.deleteProvider(name)
    }

    suspend fun toggleModel(provider: String, modelId: String, enabled: Boolean) {
        apiService.toggleModel(provider, ModelToggleRequest(modelId, enabled))
    }

    /** Bulk on/off for every non-free model of one provider — returns how
     * many models the server actually touched, so the UI can confirm the
     * bulk action really did something. */
    suspend fun toggleAllPaidModels(provider: String, enabled: Boolean): Int =
        apiService.toggleAllPaidModels(provider, ModelTogglePaidRequest(enabled)).count
}

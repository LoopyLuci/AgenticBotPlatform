package com.botserver.mobile.data

import com.botserver.mobile.data.dto.ConfigResponse
import com.botserver.mobile.data.dto.ModelsResponse
import com.botserver.mobile.data.dto.configSetRequest
import javax.inject.Inject
import javax.inject.Singleton

/** Mirrors the desktop dashboard's Control Center — default backend,
 * per-backend models, feature toggles, agent control mode — all through
 * the same generic POST /api/config/set {path, value} the dashboard uses,
 * so a change made here is a change to the one config file the server and
 * every other connected device (desktop, other phones) all read from. */
@Singleton
class SettingsRepository @Inject constructor(private val apiService: ApiService) {
    suspend fun config(): ConfigResponse = apiService.config()

    suspend fun models(): ModelsResponse = apiService.models()

    suspend fun setDefaultBackend(backend: String) {
        apiService.setConfig(configSetRequest(listOf("default_backend"), backend))
    }

    suspend fun setAgentControlMode(mode: String) {
        apiService.setConfig(configSetRequest(listOf("agent_control", "mode"), mode))
    }

    suspend fun setModel(backend: String, model: String?) {
        apiService.setConfig(configSetRequest(listOf("backends", backend, "model"), model))
    }

    suspend fun setUiAutomationEnabled(enabled: Boolean) {
        apiService.setConfig(configSetRequest(listOf("features", "ui_automation_enabled"), enabled))
    }

    suspend fun setConfirmDestructive(enabled: Boolean) {
        apiService.setConfig(configSetRequest(listOf("security", "confirm_destructive"), enabled))
    }

    suspend fun setVerboseTelemetry(enabled: Boolean) {
        apiService.setConfig(configSetRequest(listOf("features", "verbose_telemetry"), enabled))
    }
}

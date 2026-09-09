package com.botserver.mobile.data

import com.botserver.mobile.data.dto.AddHookRequest
import com.botserver.mobile.data.dto.AgentSettings
import com.botserver.mobile.data.dto.AutoManageConfig
import com.botserver.mobile.data.dto.BotInstance
import com.botserver.mobile.data.dto.HookInfo
import com.botserver.mobile.data.dto.SetAgentSettingsRequest
import com.botserver.mobile.data.dto.SetAutoManageRequest
import javax.inject.Inject
import javax.inject.Singleton

/** Full parity with the desktop dashboard's Automation section (hooks +
 * agent settings + auto-manage) — network-only, same reasoning as
 * ProvidersRepository: one small config surface each, no offline write
 * path, no pagination that would justify a local cache. */
@Singleton
class AutomationRepository @Inject constructor(private val apiService: ApiService) {
    /** The instance picker shared by all three cards — a plain network
     * fetch (not BotsRepository's Room-backed one) since this screen only
     * ever needs id+name for a dropdown, not the full edit-form shape. */
    suspend fun instances(): List<BotInstance> = apiService.bots()

    // ----------------------------------------------------------- hooks ---
    suspend fun hooks(): List<HookInfo> = apiService.hooks().hooks

    suspend fun addHook(event: String, command: String, matcher: String?, instanceId: Int?) {
        apiService.addHook(AddHookRequest(event = event, command = command, matcher = matcher, instanceId = instanceId))
    }

    suspend fun setHookEnabled(hookId: Int, enabled: Boolean) {
        if (enabled) apiService.enableHook(hookId) else apiService.disableHook(hookId)
    }

    suspend fun removeHook(hookId: Int) {
        apiService.deleteHook(hookId)
    }

    // --------------------------------------------------- agent settings ---
    suspend fun agentSettings(instanceId: Int?): AgentSettings = apiService.agentSettings(instanceId)

    suspend fun setAgentSettings(request: SetAgentSettingsRequest): AgentSettings = apiService.setAgentSettings(request)

    // ----------------------------------------------------- auto-manage ---
    suspend fun autoManage(instanceId: Int): AutoManageConfig = apiService.autoManage(instanceId)

    suspend fun setAutoManage(instanceId: Int, request: SetAutoManageRequest): AutoManageConfig =
        apiService.setAutoManage(instanceId, request)
}

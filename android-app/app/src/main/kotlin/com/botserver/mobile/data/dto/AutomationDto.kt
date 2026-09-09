package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

// ------------------------------------------------------------------ hooks
// Mirrors bot/agent_runtime/hooks.py + GET/POST /api/hooks — a local
// command run on PreToolUse/PostToolUse/SessionStart/UserPromptSubmit.
// See bot/dashboard/static/dashboard.html's "Automation" section for the
// desktop-parity UI this mirrors.
@Serializable
data class HookInfo(
    val id: Int,
    val event: String,
    val matcher: String? = null,
    val command: String,
    @SerialName("instance_id") val instanceId: Int? = null,
    val enabled: Boolean,
)

@Serializable
data class HooksListResponse(val hooks: List<HookInfo> = emptyList())

@Serializable
data class AddHookRequest(
    val event: String,
    val command: String,
    val matcher: String? = null,
    @SerialName("instance_id") val instanceId: Int? = null,
)

// --------------------------------------------------------- agent settings
// Mirrors bot/agent_settings.py's FIELDS + GET/POST /api/agent-settings.
// A null instance_id on the request targets the process-wide default row
// every instance with nothing configured falls back to. A field sent as
// null explicitly clears it back to "inherit" (see agent_settings.set_settings's
// own doc) — kotlinx encodes nulls by default (no explicitNulls=false
// configured in NetworkModule's Json), so this Just Works.
@Serializable
data class AgentSettings(
    @SerialName("max_concurrent_children") val maxConcurrentChildren: Int? = null,
    @SerialName("worker_provider") val workerProvider: String? = null,
    @SerialName("worker_model") val workerModel: String? = null,
    @SerialName("worker_effort") val workerEffort: String? = null,
    @SerialName("manager_effort") val managerEffort: String? = null,
    @SerialName("fallback_provider") val fallbackProvider: String? = null,
    @SerialName("fallback_model") val fallbackModel: String? = null,
    @SerialName("require_plan_approval") val requirePlanApproval: Boolean = false,
    // Marks the ONE instance whose tool loop gets the admin_* tool set
    // (Server Chat / Telegram admin pipeline). This screen is one of the
    // few surfaces allowed to set it at all — an instance's own tool loop
    // explicitly refuses to accept this field from itself.
    @SerialName("is_admin_instance") val isAdminInstance: Boolean = false,
)

@Serializable
data class SetAgentSettingsRequest(
    @SerialName("instance_id") val instanceId: Int? = null,
    @SerialName("max_concurrent_children") val maxConcurrentChildren: Int? = null,
    @SerialName("worker_provider") val workerProvider: String? = null,
    @SerialName("worker_model") val workerModel: String? = null,
    @SerialName("worker_effort") val workerEffort: String? = null,
    @SerialName("manager_effort") val managerEffort: String? = null,
    @SerialName("fallback_provider") val fallbackProvider: String? = null,
    @SerialName("fallback_model") val fallbackModel: String? = null,
    @SerialName("require_plan_approval") val requirePlanApproval: Boolean = false,
    @SerialName("is_admin_instance") val isAdminInstance: Boolean = false,
)

// ------------------------------------------------------------ auto-manage
// Mirrors bot/auto_manage.py + GET/POST /api/auto-manage/{instanceId}.
// Runs through the same agent-loop engine every ordinary prompt does.
@Serializable
data class AutoManageConfig(
    val enabled: Boolean = false,
    val trigger: String? = null,
    val interval: String? = null,
    @SerialName("chat_id") val chatId: String? = null,
    @SerialName("thread_id") val threadId: String? = null,
    @SerialName("goal_template") val goalTemplate: String? = null,
    @SerialName("schedule_id") val scheduleId: Int? = null,
)

/** One request DTO for both actions the route supports: [enabled] = true
 * with the other fields enables it (creates a real scheduled_commands
 * row when trigger is scheduled/both); [enabled] = false disables it
 * (and removes that row); [enabled] = null saves trigger/interval/
 * chat_id/thread_id/goal_template without touching whether it's on —
 * the route only branches on enabled being literally True or False,
 * anything else (including a present-but-null key) falls through to the
 * plain merge, matching api_auto_manage_set's own three-way dispatch. */
@Serializable
data class SetAutoManageRequest(
    val enabled: Boolean? = null,
    val trigger: String? = null,
    val interval: String? = null,
    @SerialName("chat_id") val chatId: String? = null,
    @SerialName("thread_id") val threadId: String? = null,
    @SerialName("goal_template") val goalTemplate: String? = null,
)

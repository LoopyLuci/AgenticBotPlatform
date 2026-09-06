package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Mirrors one row from GET /api/jobs — see bot/db.py's jobs table. */
@Serializable
data class JobSummary(
    val id: Int,
    @SerialName("action_type") val actionType: String,
    val backend: String,
    val status: String,
    // Free text in practice, not a numeric id — e.g. the literal string
    // "dashboard" for server-initiated jobs (see bot/support_bot/engine.py's
    // CmdContext(user_id="dashboard", ...)), or a platform user id string
    // for others. Was typed Int? before, which threw a kotlinx.serialization
    // exception ("Unexpected symbol 'd' in numeric literal") the instant any
    // job with a non-numeric user_id appeared, breaking the whole Jobs list.
    @SerialName("user_id") val userId: String? = null,
    val prompt: String? = null,
    val result: String? = null,
    val error: String? = null,
    val tokens: Int? = null,
    @SerialName("created_at") val createdAt: String,
    @SerialName("started_at") val startedAt: String? = null,
    @SerialName("finished_at") val finishedAt: String? = null,
    @SerialName("duration_ms") val durationMs: Int? = null,
    @SerialName("instance_id") val instanceId: Int? = null,
)

/** The "job_update" push over /api/ws — see bot/dashboard/server.py's
 * _on_job_changed(). Fired on create + every status transition
 * (queued -> running -> retrying? -> success|failed). */
@Serializable
data class JobUpdatePush(
    val type: String,
    val job: JobSummary,
)

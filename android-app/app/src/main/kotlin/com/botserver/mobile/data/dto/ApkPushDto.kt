package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Mirrors GET /api/android/apk/pending — see bot/db.py's apk_pushes table
 * and bot/dashboard/server.py's api_android_apk_pending(). */
@Serializable
data class PendingApkResponse(
    val available: Boolean,
    @SerialName("push_id") val pushId: Int? = null,
    @SerialName("version_label") val versionLabel: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
)

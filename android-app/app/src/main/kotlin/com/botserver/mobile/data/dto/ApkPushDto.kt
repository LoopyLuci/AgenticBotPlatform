package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Mirrors GET /api/android/apk/pending — see bot/db.py's apk_pushes table
 * and bot/dashboard/server.py's api_android_apk_pending(). `mesh`, when
 * present, means the file lives on another paired device, not this
 * server's disk — connect to `host:port` directly and present `token` to
 * redeem it, falling back to GET /api/android/apk/download/{pushId} only
 * if that direct connection fails. */
@Serializable
data class PendingApkResponse(
    val available: Boolean,
    @SerialName("push_id") val pushId: Int? = null,
    @SerialName("version_label") val versionLabel: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
    val mesh: MeshOrigin? = null,
)

@Serializable
data class MeshOrigin(
    val host: String,
    val port: Int,
    val token: String,
)

/** Body for POST /api/android/apk/send — same request the desktop
 * dashboard's per-device "Send APK" button already makes; only auth
 * changed (any paired device's api key now works, not just the desktop
 * dashboard token) so this app's Devices screen can make it too. */
@Serializable
data class ApkSendRequest(@SerialName("api_key_id") val apiKeyId: Int, val mesh: Boolean = false)

@Serializable
data class ApkSendAllRequest(val mesh: Boolean = false)

@Serializable
data class ApkSendResponse(val ok: Boolean = false, @SerialName("push_id") val pushId: Int? = null)

@Serializable
data class ApkSendAllResponse(val ok: Boolean = false, @SerialName("sent_to") val sentTo: Int = 0)

/** Body for POST /api/android/apk/mesh/redeem — sent by *this* device's own
 * MeshServer, not by whoever it's talking to on the socket, right after
 * accepting an incoming connection and reading the token it presented. */
@Serializable
data class MeshRedeemRequest(@SerialName("push_id") val pushId: Int, val token: String)

@Serializable
data class MeshRedeemResponse(val ok: Boolean = false)

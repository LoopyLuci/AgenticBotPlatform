package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Mirrors one entry from GET /api/devices / the "device_list" WebSocket
 * broadcast — see bot/dashboard/server.py's _annotate_online() and
 * db.list_devices(). `online` is computed server-side from last_seen so
 * every client (desktop, this app) agrees on the same freshness window. */
@Serializable
data class DeviceInfo(
    val id: Int,
    val label: String,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("last_used_at") val lastUsedAt: String? = null,
    val platform: String? = null,
    @SerialName("app_version") val appVersion: String? = null,
    @SerialName("device_model") val deviceModel: String? = null,
    @SerialName("os_version") val osVersion: String? = null,
    @SerialName("last_seen") val lastSeen: String? = null,
    val online: Boolean = false,
)

@Serializable
data class DeviceListMessage(
    val type: String,
    val devices: List<DeviceInfo> = emptyList(),
)

package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Mirrors POST /api/mobile-keys — bot/dashboard/server.py's
 * api_mobile_keys_create(). host/host2 are optional, independent paths to
 * this server (see CredentialStore) embedded into the returned QR/link so
 * the new device doesn't have to be told an address by hand. */
@Serializable
data class CreateMobileKeyRequest(
    val label: String,
    val host: String? = null,
    val host2: String? = null,
)

@Serializable
data class CreateMobileKeyResponse(
    val id: Int,
    val label: String,
    val key: String,
    @SerialName("qr_png_base64") val qrPngBase64: String,
)

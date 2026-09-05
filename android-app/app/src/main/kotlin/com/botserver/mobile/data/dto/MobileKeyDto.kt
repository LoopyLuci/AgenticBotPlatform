package com.botserver.mobile.data.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Mirrors POST /api/mobile-keys — bot/dashboard/server.py's
 * api_mobile_keys_create(). host/host2/host3 are optional, independent
 * paths to this server (see CredentialStore) embedded into the returned
 * pairing_code/QR so the new device doesn't have to be told an address
 * by hand. */
@Serializable
data class CreateMobileKeyRequest(
    val label: String,
    val host: String? = null,
    val host2: String? = null,
    val host3: String? = null,
)

@Serializable
data class CreateMobileKeyResponse(
    val id: Int,
    val label: String,
    val key: String,
    // The single self-contained string (see bot/mobile_pairing.py) that
    // already carries every host baked in — prefer this over
    // reconstructing a botserver://pair?... URI client-side, since the
    // server is the one that resolved which hosts actually apply.
    @SerialName("pairing_code") val pairingCode: String,
    @SerialName("qr_png_base64") val qrPngBase64: String,
)

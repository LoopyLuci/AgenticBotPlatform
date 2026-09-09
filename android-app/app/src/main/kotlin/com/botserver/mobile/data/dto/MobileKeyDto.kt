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
    // "none" | "standard" | "elevated" | "unrestricted" — server refuses
    // (403) any value ranked above this device's own tier; see
    // bot/device_tiers.py's can_mint(). Defaults to "none" so a caller
    // that leaves this out mints exactly as before this field existed.
    val tier: String = "none",
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

/** POST /api/mobile-keys/{id}/tier — bot/dashboard/server.py's
 * api_mobile_keys_set_tier(). Server re-checks can_manage/can_mint itself;
 * this is never trusted client-side beyond deciding whether to *offer*
 * the action. */
@Serializable
data class SetDeviceTierRequest(val tier: String)

@Serializable
data class SetDeviceTierResponse(
    val ok: Boolean,
    @SerialName("permission_tier") val permissionTier: String,
)

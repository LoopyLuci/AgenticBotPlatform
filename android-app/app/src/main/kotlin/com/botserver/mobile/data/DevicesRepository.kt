package com.botserver.mobile.data

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Base64
import com.botserver.mobile.data.dto.CreateMobileKeyRequest
import com.botserver.mobile.data.dto.DeviceInfo
import kotlinx.coroutines.flow.Flow
import javax.inject.Inject
import javax.inject.Singleton

data class NewDevicePairing(
    val label: String,
    val pairingCode: String,
    val qr: Bitmap,
)

/** Lets an already-paired device mint a pairing key for a *new* device —
 * the mobile equivalent of the dashboard's Mobile tab "Generate a key",
 * except this device already knows a real, working host/host2 for this
 * server (see CredentialStore), so there's nothing to type by hand. */
@Singleton
class DevicesRepository @Inject constructor(
    private val apiService: ApiService,
    private val credentials: CredentialStore,
    private val liveEvents: LiveEventsClient,
) {
    suspend fun createPairingForNewDevice(label: String): NewDevicePairing {
        // Pass this device's own known-working hosts as a starting point
        // (it already has a real, working host/host2/host3 for this
        // server — see CredentialStore) — but the server still resolves
        // and returns the actual self-contained pairing_code, so any
        // slot this device left blank still gets auto-filled server-side
        // exactly like the dashboard's own "Generate a key" does.
        val res = apiService.createMobileKey(
            CreateMobileKeyRequest(label = label, host = credentials.host, host2 = credentials.host2, host3 = credentials.host3),
        )
        val bytes = Base64.decode(res.qrPngBase64, Base64.DEFAULT)
        val qr = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            ?: error("Server returned an unreadable QR image.")
        return NewDevicePairing(label = res.label, pairingCode = res.pairingCode, qr = qr)
    }

    /** One-shot snapshot for initial screen load — see /api/devices in
     * bot/dashboard/server.py. */
    suspend fun devices(): List<DeviceInfo> = apiService.devices()

    /** Live deltas — now just a filtered view of the app's one shared
     * LiveEventsClient connection (see its doc) rather than this
     * repository opening its own second socket. */
    fun liveDevices(): Flow<List<DeviceInfo>> = liveEvents.deviceList
}

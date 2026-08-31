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
    val key: String,
    val host: String?,
    val host2: String?,
    val qr: Bitmap,
) {
    /** Same shape PairingRepository.parse() already understands — what a
     * "Share pairing link" action hands off via ACTION_SEND. */
    fun pairUri(): String {
        val params = mutableListOf("key=$key")
        if (!host.isNullOrBlank()) params.add(0, "host=$host")
        if (!host2.isNullOrBlank()) params.add("host2=$host2")
        return "botserver://pair?" + params.joinToString("&")
    }
}

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
        val res = apiService.createMobileKey(
            CreateMobileKeyRequest(label = label, host = credentials.host, host2 = credentials.host2),
        )
        val bytes = Base64.decode(res.qrPngBase64, Base64.DEFAULT)
        val qr = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            ?: error("Server returned an unreadable QR image.")
        return NewDevicePairing(label = res.label, key = res.key, host = credentials.host, host2 = credentials.host2, qr = qr)
    }

    /** One-shot snapshot for initial screen load — see /api/devices in
     * bot/dashboard/server.py. */
    suspend fun devices(): List<DeviceInfo> = apiService.devices()

    /** Live deltas — now just a filtered view of the app's one shared
     * LiveEventsClient connection (see its doc) rather than this
     * repository opening its own second socket. */
    fun liveDevices(): Flow<List<DeviceInfo>> = liveEvents.deviceList
}

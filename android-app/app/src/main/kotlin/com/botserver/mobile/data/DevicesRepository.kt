package com.botserver.mobile.data

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Base64
import com.botserver.mobile.data.dto.CreateMobileKeyRequest
import com.botserver.mobile.data.dto.DeviceInfo
import com.botserver.mobile.data.dto.DeviceListMessage
import com.botserver.mobile.di.PLACEHOLDER_BASE_URL
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
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
    private val client: OkHttpClient,
    private val json: Json,
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

    /** Live deltas over the same authenticated, host-failover-aware
     * OkHttpClient Retrofit uses (see di/NetworkModule.kt's
     * DynamicHostInterceptor, which rewrites this placeholder host to
     * whichever of host/host2 last worked, and attaches the
     * X-Dashboard-Token header to every request including this one's
     * handshake — no need to put the token in the URL, unlike the desktop
     * client's browser WebSocket, which can't set a custom header). Emits
     * nothing further and completes if this device isn't paired yet. */
    fun liveDevices(): Flow<List<DeviceInfo>> = callbackFlow {
        val apiKey = credentials.apiKey
        if (apiKey.isNullOrBlank()) {
            close()
            return@callbackFlow
        }
        val url = "$PLACEHOLDER_BASE_URL/api/ws".toHttpUrlOrNull()
            ?: run { close(); return@callbackFlow }
        val request = Request.Builder().url(url).build()
        val listener = object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                runCatching { json.decodeFromString(DeviceListMessage.serializer(), text) }
                    .onSuccess { msg -> if (msg.type == "device_list") trySend(msg.devices) }
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                close(t)
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                close()
            }
        }
        val socket = client.newWebSocket(request, listener)
        awaitClose { socket.close(1000, null) }
    }
}

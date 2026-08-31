package com.botserver.mobile.data

import com.botserver.mobile.data.dto.ChatMessagePush
import com.botserver.mobile.data.dto.DeviceInfo
import com.botserver.mobile.data.dto.DeviceListMessage
import com.botserver.mobile.data.dto.JobUpdatePush
import com.botserver.mobile.di.PLACEHOLDER_BASE_URL
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.mapNotNull
import kotlinx.coroutines.flow.shareIn
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import javax.inject.Inject
import javax.inject.Singleton

/** The one live-events WebSocket connection to /api/ws, shared by every
 * feature that wants push updates (device presence, chat, jobs) instead
 * of each opening — and separately reconnecting/backing off — its own
 * socket. Connects only while at least one of [deviceList]/[chatMessages]/
 * [jobUpdates] has an active collector ([SharingStarted.WhileSubscribed]),
 * and reconnects with exponential backoff (capped at 30s) on any drop —
 * the previous per-feature sockets (see DevicesRepository's prior
 * liveDevices()) reconnected on a naive flat 4s retry with no backoff.
 *
 * Auth: the same X-Dashboard-Token header DynamicHostInterceptor attaches
 * to every request goes out on this handshake too — this client only
 * needs the placeholder URL and lets that interceptor rewrite it,
 * matching every other request in the app (see NetworkModule.kt). */
@Singleton
class LiveEventsClient @Inject constructor(
    private val client: OkHttpClient,
    private val json: Json,
) {
    private val scope = CoroutineScope(SupervisorJob())

    private val rawEvents: Flow<JsonObject> = callbackFlow {
        var currentSocket: WebSocket? = null
        val reconnectJob = launch {
            var backoffMs = 1000L
            while (isActive) {
                val url = "$PLACEHOLDER_BASE_URL/api/ws".toHttpUrlOrNull()
                if (url == null) {
                    delay(2000)
                    continue
                }
                val closed = Channel<Unit>(Channel.CONFLATED)
                val listener = object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        backoffMs = 1000L
                    }
                    override fun onMessage(webSocket: WebSocket, text: String) {
                        runCatching { json.parseToJsonElement(text) as? JsonObject }
                            .getOrNull()?.let { trySend(it) }
                    }
                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        closed.trySend(Unit)
                    }
                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                        closed.trySend(Unit)
                    }
                }
                currentSocket = client.newWebSocket(Request.Builder().url(url).build(), listener)
                closed.receive()
                currentSocket = null
                delay(backoffMs)
                backoffMs = (backoffMs * 2).coerceAtMost(30_000)
            }
        }
        awaitClose {
            reconnectJob.cancel()
            currentSocket?.cancel()
        }
    }.shareIn(scope, SharingStarted.WhileSubscribed(stopTimeoutMillis = 5000), replay = 0)

    private fun JsonObject.typeIs(t: String) = this["type"]?.jsonPrimitive?.content == t

    val deviceList: Flow<List<DeviceInfo>> = rawEvents
        .filter { it.typeIs("device_list") }
        .mapNotNull { runCatching { json.decodeFromJsonElement(DeviceListMessage.serializer(), it).devices }.getOrNull() }

    val chatMessages: Flow<ChatMessagePush> = rawEvents
        .filter { it.typeIs("chat_message") }
        .mapNotNull { runCatching { json.decodeFromJsonElement(ChatMessagePush.serializer(), it) }.getOrNull() }

    val jobUpdates: Flow<JobUpdatePush> = rawEvents
        .filter { it.typeIs("job_update") }
        .mapNotNull { runCatching { json.decodeFromJsonElement(JobUpdatePush.serializer(), it) }.getOrNull() }
}

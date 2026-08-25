package com.botserver.mobile.data

import android.content.Context
import android.util.Log
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.webrtc.DataChannel
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.MediaStream
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.RtpReceiver
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import java.io.File
import java.nio.ByteBuffer
import java.util.concurrent.ConcurrentHashMap
import javax.inject.Inject
import javax.inject.Singleton
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

private const val TAG = "WebRtcMesh"
private const val CHUNK_BYTES = 16 * 1024
private const val BUFFERED_AMOUNT_HIGH_WATER = 1 * 1024 * 1024

/** No-op base so call sites only override the one or two callbacks they
 * actually care about — org.webrtc's Java interfaces have no default
 * methods, and implementing all of them inline at every use site would
 * bury the actual logic. */
private open class SimpleSdpObserver : SdpObserver {
    override fun onCreateSuccess(desc: SessionDescription?) {}
    override fun onSetSuccess() {}
    override fun onCreateFailure(error: String?) {}
    override fun onSetFailure(error: String?) {}
}

private open class SimplePeerConnectionObserver : PeerConnection.Observer {
    override fun onSignalingChange(state: PeerConnection.SignalingState?) {}
    override fun onIceConnectionChange(state: PeerConnection.IceConnectionState?) {}
    override fun onIceConnectionReceivingChange(receiving: Boolean) {}
    override fun onIceGatheringChange(state: PeerConnection.IceGatheringState?) {}
    override fun onIceCandidate(candidate: IceCandidate?) {}
    override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>?) {}
    override fun onAddStream(stream: MediaStream?) {}
    override fun onRemoveStream(stream: MediaStream?) {}
    override fun onDataChannel(channel: DataChannel?) {}
    override fun onRenegotiationNeeded() {}
    override fun onAddTrack(receiver: RtpReceiver?, streams: Array<out MediaStream>?) {}
}

private suspend fun PeerConnection.createOfferSuspend(constraints: MediaConstraints): SessionDescription =
    suspendCancellableCoroutine { cont ->
        createOffer(object : SimpleSdpObserver() {
            override fun onCreateSuccess(desc: SessionDescription?) {
                if (desc != null) cont.resume(desc) else cont.resumeWithException(IllegalStateException("null offer"))
            }
            override fun onCreateFailure(error: String?) = cont.resumeWithException(IllegalStateException(error))
        }, constraints)
    }

private suspend fun PeerConnection.createAnswerSuspend(constraints: MediaConstraints): SessionDescription =
    suspendCancellableCoroutine { cont ->
        createAnswer(object : SimpleSdpObserver() {
            override fun onCreateSuccess(desc: SessionDescription?) {
                if (desc != null) cont.resume(desc) else cont.resumeWithException(IllegalStateException("null answer"))
            }
            override fun onCreateFailure(error: String?) = cont.resumeWithException(IllegalStateException(error))
        }, constraints)
    }

private suspend fun PeerConnection.setLocalDescriptionSuspend(desc: SessionDescription) =
    suspendCancellableCoroutine<Unit> { cont ->
        setLocalDescription(object : SimpleSdpObserver() {
            override fun onSetSuccess() = cont.resume(Unit)
            override fun onSetFailure(error: String?) = cont.resumeWithException(IllegalStateException(error))
        }, desc)
    }

private suspend fun PeerConnection.setRemoteDescriptionSuspend(desc: SessionDescription) =
    suspendCancellableCoroutine<Unit> { cont ->
        setRemoteDescription(object : SimpleSdpObserver() {
            override fun onSetSuccess() = cont.resume(Unit)
            override fun onSetFailure(error: String?) = cont.resumeWithException(IllegalStateException(error))
        }, desc)
    }

/** One in-flight negotiation with one peer, either role. Candidates that
 * arrive over the signaling relay before the remote description is set
 * (a routine race in WebRTC — ICE gathering starts immediately and often
 * beats the signaling round trip) are queued and flushed once it is. */
private class Negotiation(val peerConnection: PeerConnection) {
    var remoteDescriptionSet = false
    val pendingRemoteCandidates = mutableListOf<IceCandidate>()
    var dataChannel: DataChannel? = null
}

/** The WAN half of the mesh APK transfer's "hybrid" transport (see
 * MeshServer.kt for the LAN half). Two devices that can't reach each other
 * directly on a local network instead exchange SDP/ICE through the
 * server's existing /api/ws socket (pure relay — the server never sees a
 * byte of the actual transfer) and, once a PeerConnection is up, run the
 * exact same push_id/token request-and-stream protocol over a DataChannel
 * that MeshServer runs over a raw TCP socket. ICE servers are STUN plus,
 * when the operator has configured one (bot/turn.py), a real TURN relay
 * fetched fresh per connection via GET /api/turn/credentials — see
 * docs/turn-server-setup.md for standing up the coturn side. Without a
 * TURN server configured, this still won't succeed against a symmetric
 * NAT/restrictive firewall on either side; that's a real, documented limit,
 * not a silent failure (the caller falls back to nothing further and
 * reports a clear error). */
@Singleton
class WebRtcMeshClient @Inject constructor(
    private val apiService: ApiService,
    private val credentials: CredentialStore,
    private val httpClient: OkHttpClient,
    private val json: Json,
    @ApplicationContext private val context: Context,
) {
    private val scope = CoroutineScope(Dispatchers.IO + Job())
    private var signalingSocket: WebSocket? = null
    private val negotiations = ConcurrentHashMap<Int, Negotiation>()
    private val incomingOffers = ConcurrentHashMap<Int, SessionDescription>()

    private val factory: PeerConnectionFactory by lazy {
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(context.applicationContext).createInitializationOptions(),
        )
        PeerConnectionFactory.builder().createPeerConnectionFactory()
    }

    /** STUN always; TURN too when the server has one configured — fetched
     * fresh (not cached) since this only runs once per PeerConnection and a
     * transfer is far shorter than the credential's ttl, so there's no
     * benefit to caching that would outweigh the risk of using a stale/
     * expired credential. A TURN fetch failure (server has none configured,
     * or the request itself fails) just falls back to STUN-only rather than
     * failing the whole connection attempt. */
    private suspend fun iceServers(): List<PeerConnection.IceServer> {
        val stun = PeerConnection.IceServer.builder("stun:stun.l.google.com:19302").createIceServer()
        val turn = runCatching { apiService.turnCredentials() }.getOrNull()
        if (turn == null || !turn.enabled || turn.urls.isEmpty() || turn.username == null || turn.credential == null) {
            return listOf(stun)
        }
        val turnServer = PeerConnection.IceServer.builder(turn.urls)
            .setUsername(turn.username)
            .setPassword(turn.credential)
            .createIceServer()
        return listOf(stun, turnServer)
    }

    private suspend fun buildRtcConfig(): PeerConnection.RTCConfiguration =
        PeerConnection.RTCConfiguration(iceServers()).apply { sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN }

    /** Opens the signaling relay and starts answering any incoming offer —
     * called alongside MeshServer.start() so this device can serve its own
     * APK to a peer on a different network for as long as the Devices
     * screen (and thus mesh participation) is open. */
    fun start() {
        if (signalingSocket != null) return
        val apiKey = credentials.apiKey ?: return
        val wsBase = credentials.preferredBaseUrl().replaceFirst("http", "ws")
        val request = Request.Builder().url("$wsBase/api/ws?token=$apiKey").build()
        signalingSocket = httpClient.newWebSocket(request, object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                scope.launch { handleSignal(text) }
            }
        })
    }

    fun stop() {
        signalingSocket?.close(1000, null)
        signalingSocket = null
        negotiations.values.forEach { it.peerConnection.close() }
        negotiations.clear()
        incomingOffers.clear()
    }

    private fun sendSignal(toApiKeyId: Int, data: JsonObject) {
        val envelope = buildJsonObject {
            put("type", "webrtc_signal")
            put("to_api_key_id", toApiKeyId)
            put("data", data)
        }
        signalingSocket?.send(envelope.toString())
    }

    private suspend fun handleSignal(text: String) {
        val message = runCatching { json.parseToJsonElement(text).jsonObject }.getOrNull() ?: return
        if (message["type"]?.jsonPrimitive?.contentOrNull != "webrtc_signal") return
        val fromId = message["from_api_key_id"]?.jsonPrimitive?.intOrNull ?: return
        val data = message["data"]?.jsonObject ?: return
        when (data["kind"]?.jsonPrimitive?.contentOrNull) {
            "offer" -> onOfferReceived(fromId, data)
            "answer" -> onAnswerReceived(fromId, data)
            "ice" -> onIceReceived(fromId, data)
        }
    }

    // ---------------------------------------------------------- origin side
    // This device is being asked to serve its own APK — accept the offer,
    // answer, and once the resulting DataChannel opens, wait for the
    // push_id/token request and validate it with the server exactly like
    // MeshServer.kt's TCP listener does, before streaming a byte.

    private suspend fun onOfferReceived(fromId: Int, data: JsonObject) {
        val sdp = data["sdp"]?.jsonPrimitive?.contentOrNull ?: return
        val pc = factory.createPeerConnection(buildRtcConfig(), object : SimplePeerConnectionObserver() {
            override fun onIceCandidate(candidate: IceCandidate?) {
                candidate ?: return
                sendSignal(fromId, iceToJson(candidate))
            }
            override fun onDataChannel(channel: DataChannel?) {
                channel ?: return
                negotiations[fromId]?.dataChannel = channel
                channel.registerObserver(originChannelObserver(channel))
            }
        }) ?: return
        val negotiation = Negotiation(pc)
        negotiations[fromId] = negotiation
        pc.setRemoteDescriptionSuspend(SessionDescription(SessionDescription.Type.OFFER, sdp))
        negotiation.remoteDescriptionSet = true
        negotiation.pendingRemoteCandidates.forEach { pc.addIceCandidate(it) }
        negotiation.pendingRemoteCandidates.clear()
        val answer = pc.createAnswerSuspend(MediaConstraints())
        pc.setLocalDescriptionSuspend(answer)
        sendSignal(fromId, buildJsonObject { put("kind", "answer"); put("sdp", answer.description) })
    }

    private fun originChannelObserver(channel: DataChannel) = object : DataChannel.Observer {
        var expectedLength: Long? = null
        var sentBytes = 0L
        var apkBytes: ByteArray? = null

        override fun onBufferedAmountChange(previousAmount: Long) {}
        override fun onStateChange() {}
        override fun onMessage(buffer: DataChannel.Buffer) {
            if (buffer.binary) return // origin only expects the one text control message
            val bytes = ByteArray(buffer.data.remaining())
            buffer.data.get(bytes)
            val requestObj = runCatching { json.parseToJsonElement(String(bytes, Charsets.UTF_8)).jsonObject }.getOrNull() ?: return
            val pushId = requestObj["push_id"]?.jsonPrimitive?.intOrNull ?: return
            val token = requestObj["token"]?.jsonPrimitive?.contentOrNull ?: return
            scope.launch { serveApk(channel, pushId, token) }
        }
    }

    private suspend fun serveApk(channel: DataChannel, pushId: Int, token: String) {
        val redeemed = runCatching {
            apiService.redeemMeshToken(com.botserver.mobile.data.dto.MeshRedeemRequest(pushId, token)).ok
        }.getOrDefault(false)
        if (!redeemed) {
            channel.send(textBuffer("""{"error":"invalid_token"}"""))
            channel.close()
            return
        }
        val apkFile = File(context.applicationInfo.sourceDir)
        val bytes = apkFile.readBytes()
        channel.send(textBuffer("""{"length":${bytes.size}}"""))
        var offset = 0
        while (offset < bytes.size) {
            // Backpressure: SCTP's send buffer can queue faster than the
            // wire drains it, especially over a slow WAN path — without
            // this, a large file can pile megabytes into memory-backed
            // buffers before the channel throttles anything on its own.
            while (channel.bufferedAmount() > BUFFERED_AMOUNT_HIGH_WATER) {
                kotlinx.coroutines.delay(20)
            }
            val end = minOf(offset + CHUNK_BYTES, bytes.size)
            val chunk = ByteBuffer.wrap(bytes, offset, end - offset).slice()
            channel.send(DataChannel.Buffer(chunk, true))
            offset = end
        }
    }

    // -------------------------------------------------------- receiver side
    // This device wants another device's APK — used only when the direct
    // LAN socket (MeshServer/connectToMeshPeer) wasn't reachable at all.

    suspend fun download(originApiKeyId: Int, pushId: Int, token: String, destFile: File): File? {
        if (signalingSocket == null) start()
        return withTimeoutOrNull(20_000) {
            runCatching { downloadInternal(originApiKeyId, pushId, token, destFile) }
                .onFailure { e -> Log.w(TAG, "webrtc transfer from device $originApiKeyId failed: ${e.message}") }
                .getOrNull()
        }
    }

    private suspend fun downloadInternal(originApiKeyId: Int, pushId: Int, token: String, destFile: File): File {
        val rtcConfig = buildRtcConfig() // must happen before suspendCancellableCoroutine — its lambda isn't a suspend context
        return suspendCancellableCoroutine { cont ->
            var expectedLength = -1L
            val received = java.io.ByteArrayOutputStream()

            val pc = factory.createPeerConnection(rtcConfig, object : SimplePeerConnectionObserver() {
                override fun onIceCandidate(candidate: IceCandidate?) {
                    candidate ?: return
                    sendSignal(originApiKeyId, iceToJson(candidate))
                }
            }) ?: run { cont.resumeWithException(IllegalStateException("could not create PeerConnection")); return@suspendCancellableCoroutine }

            val negotiation = Negotiation(pc)
            negotiations[originApiKeyId] = negotiation

            val channel = pc.createDataChannel("apk", DataChannel.Init())
            negotiation.dataChannel = channel
            channel.registerObserver(object : DataChannel.Observer {
                override fun onBufferedAmountChange(previousAmount: Long) {}
                override fun onStateChange() {
                    if (channel.state() == DataChannel.State.OPEN) {
                        channel.send(textBuffer("""{"push_id":$pushId,"token":"$token"}"""))
                    }
                }
                override fun onMessage(buffer: DataChannel.Buffer) {
                    if (!buffer.binary) {
                        val bytes = ByteArray(buffer.data.remaining())
                        buffer.data.get(bytes)
                        val obj = runCatching { json.parseToJsonElement(String(bytes, Charsets.UTF_8)).jsonObject }.getOrNull()
                        val length = obj?.get("length")?.jsonPrimitive?.longOrNull
                        if (obj?.get("error") != null) {
                            cont.resumeWithException(IllegalStateException("origin rejected the token"))
                        } else if (length != null) {
                            expectedLength = length
                        }
                        return
                    }
                    val bytes = ByteArray(buffer.data.remaining())
                    buffer.data.get(bytes)
                    received.write(bytes)
                    if (expectedLength in 0..received.size().toLong()) {
                        destFile.writeBytes(received.toByteArray())
                        channel.close()
                        pc.close()
                        negotiations.remove(originApiKeyId)
                        if (cont.isActive) cont.resume(destFile)
                    }
                }
            })

            scope.launch {
                val offer = pc.createOfferSuspend(MediaConstraints())
                pc.setLocalDescriptionSuspend(offer)
                sendSignal(originApiKeyId, buildJsonObject { put("kind", "offer"); put("sdp", offer.description) })
            }
        }
    }

    private fun onAnswerReceived(fromId: Int, data: JsonObject) {
        val sdp = data["sdp"]?.jsonPrimitive?.contentOrNull ?: return
        val negotiation = negotiations[fromId] ?: return
        scope.launch {
            negotiation.peerConnection.setRemoteDescriptionSuspend(SessionDescription(SessionDescription.Type.ANSWER, sdp))
            negotiation.remoteDescriptionSet = true
            negotiation.pendingRemoteCandidates.forEach { negotiation.peerConnection.addIceCandidate(it) }
            negotiation.pendingRemoteCandidates.clear()
        }
    }

    private fun onIceReceived(fromId: Int, data: JsonObject) {
        val candidate = IceCandidate(
            data["sdpMid"]?.jsonPrimitive?.contentOrNull ?: "",
            data["sdpMLineIndex"]?.jsonPrimitive?.doubleOrNull?.toInt() ?: 0,
            data["candidate"]?.jsonPrimitive?.contentOrNull ?: return,
        )
        val negotiation = negotiations[fromId] ?: return
        if (negotiation.remoteDescriptionSet) negotiation.peerConnection.addIceCandidate(candidate)
        else negotiation.pendingRemoteCandidates.add(candidate)
    }

    private fun iceToJson(candidate: IceCandidate) = buildJsonObject {
        put("kind", "ice")
        put("candidate", candidate.sdp)
        put("sdpMid", candidate.sdpMid)
        put("sdpMLineIndex", candidate.sdpMLineIndex)
    }

    private fun textBuffer(text: String) = DataChannel.Buffer(ByteBuffer.wrap(text.toByteArray(Charsets.UTF_8)), false)
}

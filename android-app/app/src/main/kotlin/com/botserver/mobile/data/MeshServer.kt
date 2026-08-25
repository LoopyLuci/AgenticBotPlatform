package com.botserver.mobile.data

import android.content.Context
import android.util.Log
import com.botserver.mobile.data.dto.MeshRedeemRequest
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import java.io.DataOutputStream
import java.io.File
import java.net.ServerSocket
import java.net.Socket
import javax.inject.Inject
import javax.inject.Singleton

/** Self-reported over X-Mesh-Port (see NetworkModule.kt's interceptor) so
 * the server can tell another device exactly where to dial this one — a
 * plain top-level holder, not Hilt-managed, so the OkHttp interceptor (built
 * once, before any request) can read it live without a circular dependency
 * on MeshServer itself. 0 means "not currently listening." */
object MeshPortHolder {
    @Volatile var port: Int = 0
}

/** The mesh transfer's actual listener: while running, any other paired
 * device that's been handed a one-time token by the server (see
 * bot/dashboard/server.py's /api/android/apk/send with mesh=true) can
 * connect directly to this device on the local network and pull this
 * device's own installed APK, without the bytes ever passing through the
 * central server. Started only while the Devices screen is open — a real,
 * working v1, not a background service; that's a deliberate scope limit,
 * not an oversight (see DevicesViewModel.startPresence/stopMesh).
 *
 * Wire protocol, deliberately minimal: the connecting peer writes one
 * UTF-8 line of JSON, `{"push_id":123,"token":"..."}`, newline-terminated.
 * This device redeems that token with the server (proving it's genuine,
 * matches this exact push, and hasn't been used before) and then either
 * writes an 8-byte big-endian length followed by the raw APK bytes, or
 * closes the connection immediately on any failure. */
@Singleton
class MeshServer @Inject constructor(
    private val apiService: ApiService,
    private val json: Json,
    @ApplicationContext private val context: Context,
) {
    private var serverSocket: ServerSocket? = null
    private var acceptJob: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO + Job())

    val isRunning: Boolean get() = serverSocket != null

    fun start() {
        if (serverSocket != null) return
        val socket = runCatching { ServerSocket(0) }.getOrNull() ?: return
        serverSocket = socket
        MeshPortHolder.port = socket.localPort
        acceptJob = scope.launch {
            while (true) {
                val client = runCatching { socket.accept() }.getOrNull() ?: break
                launch { handleConnection(client) }
            }
        }
    }

    fun stop() {
        acceptJob?.cancel()
        acceptJob = null
        runCatching { serverSocket?.close() }
        serverSocket = null
        MeshPortHolder.port = 0
    }

    private suspend fun handleConnection(client: Socket) {
        client.use { sock ->
            sock.soTimeout = 10_000
            val line = runCatching { sock.getInputStream().bufferedReader().readLine() }.getOrNull()
            if (line.isNullOrBlank()) return
            val obj = runCatching { json.parseToJsonElement(line).jsonObject }.getOrNull() ?: return
            val pushId = obj["push_id"]?.jsonPrimitive?.intOrNull ?: return
            val token = obj["token"]?.jsonPrimitive?.contentOrNull ?: return
            val redeemed = runCatching { apiService.redeemMeshToken(MeshRedeemRequest(pushId, token)).ok }.getOrDefault(false)
            if (!redeemed) return
            val apkFile = File(context.applicationInfo.sourceDir)
            val out = DataOutputStream(sock.getOutputStream())
            out.writeLong(apkFile.length())
            apkFile.inputStream().use { input -> input.copyTo(out) }
            out.flush()
        }
    }
}

private const val TAG = "MeshServer"

/** Client side of the same protocol — connects to a peer's mesh listener
 * (host:port from PendingApkResponse.mesh), presents the token, and reads
 * back the length-prefixed APK bytes. Returns null on any failure so the
 * caller can fall back to the server-relay download instead. */
suspend fun connectToMeshPeer(host: String, port: Int, pushId: Int, token: String, destFile: File): File? {
    return runCatching {
        Socket().use { sock ->
            sock.connect(java.net.InetSocketAddress(host, port), 6_000)
            sock.soTimeout = 30_000
            val request = """{"push_id":$pushId,"token":"$token"}""" + "\n"
            sock.getOutputStream().write(request.toByteArray(Charsets.UTF_8))
            sock.getOutputStream().flush()
            val input = java.io.DataInputStream(sock.getInputStream())
            val length = input.readLong()
            if (length <= 0) return null
            destFile.outputStream().use { output ->
                var remaining = length
                val buffer = ByteArray(64 * 1024)
                while (remaining > 0) {
                    val read = input.read(buffer, 0, minOf(buffer.size.toLong(), remaining).toInt())
                    if (read < 0) return null
                    output.write(buffer, 0, read)
                    remaining -= read
                }
            }
            destFile
        }
    }.onFailure { e -> Log.w(TAG, "mesh transfer from $host:$port failed: ${e.message}") }.getOrNull()
}

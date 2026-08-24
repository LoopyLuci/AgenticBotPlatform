package com.botserver.mobile.data

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import com.botserver.mobile.data.dto.ServerChatConversation
import com.botserver.mobile.data.dto.ServerChatMessage
import com.botserver.mobile.data.dto.ServerChatSendRequest
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import javax.inject.Inject
import javax.inject.Singleton

/** A permanent, bot-independent messaging/file channel between this
 * server's own devices — the desktop app and every paired phone. Separate
 * from ChatRepository (which talks to a bot_instance through a platform
 * SDK); this never touches a bot at all, just device-to-device via the
 * server's own DB. See bot/db.py's server_chat_conversations comment. */
@Singleton
class ServerChatRepository @Inject constructor(
    private val apiService: ApiService,
    @ApplicationContext private val context: Context,
) {
    /** Cached for the process lifetime — a device's own Server Chat
     * identity never changes without re-pairing. */
    private var myDeviceId: Int? = null

    suspend fun myDeviceId(): Int {
        myDeviceId?.let { return it }
        val id = apiService.serverChatWhoAmI().deviceId
        myDeviceId = id
        return id
    }

    suspend fun conversations(): List<ServerChatConversation> = apiService.serverChatConversations()

    suspend fun messages(conversationId: Int, afterId: Int = 0): List<ServerChatMessage> =
        apiService.serverChatMessages(conversationId = conversationId, afterId = afterId)

    suspend fun send(conversationId: Int, text: String) {
        apiService.serverChatSend(ServerChatSendRequest(conversationId, text))
    }

    suspend fun downloadAttachment(messageId: Int, suggestedName: String): File = withContext(Dispatchers.IO) {
        val body = apiService.downloadServerChatAttachment(messageId)
        val dir = File(context.cacheDir, "downloads").apply { mkdirs() }
        val safeName = suggestedName.ifBlank { "file" }.replace(Regex("[^A-Za-z0-9._-]+"), "_")
        val dest = File(dir, "serverchat_${messageId}_$safeName")
        body.byteStream().use { input ->
            dest.outputStream().use { output -> input.copyTo(output) }
        }
        dest
    }

    /** One-shot multipart send — Server Chat files stay local to this
     * server (no platform relay size limit to work around the way
     * ChatRepository.sendFile's chunked protocol does), so there's no need
     * for the resumable chunk-by-chunk dance. */
    suspend fun sendFile(conversationId: Int, text: String, uri: Uri) = withContext(Dispatchers.IO) {
        val resolver = context.contentResolver
        var name = "file"
        resolver.query(uri, null, null, null, null)?.use { cursor ->
            val nameIdx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (cursor.moveToFirst() && nameIdx >= 0) cursor.getString(nameIdx)?.let { name = it }
        }
        val mime = resolver.getType(uri) ?: "application/octet-stream"
        val cacheFile = File(File(context.cacheDir, "uploads").apply { mkdirs() }, name)
        resolver.openInputStream(uri)!!.use { input ->
            cacheFile.outputStream().use { output -> input.copyTo(output) }
        }
        try {
            val filePart = MultipartBody.Part.createFormData(
                "file", name, cacheFile.asRequestBody(mime.toMediaType())
            )
            apiService.serverChatSendFile(
                conversationId.toString().toRequestBody("text/plain".toMediaType()),
                text.toRequestBody("text/plain".toMediaType()),
                filePart,
            )
        } finally {
            cacheFile.delete()
        }
    }
}

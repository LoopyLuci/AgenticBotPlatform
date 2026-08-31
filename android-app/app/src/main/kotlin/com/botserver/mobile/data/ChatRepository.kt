package com.botserver.mobile.data

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import com.botserver.mobile.data.db.ChatDao
import com.botserver.mobile.data.db.ChatMessageEntity
import com.botserver.mobile.data.dto.BotInstanceSummary
import com.botserver.mobile.data.dto.ChatMessage
import com.botserver.mobile.data.dto.SendMessageRequest
import com.botserver.mobile.data.dto.SendToBotRequest
import com.botserver.mobile.data.dto.UploadInitRequest
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.io.RandomAccessFile
import javax.inject.Inject
import javax.inject.Singleton

// Client-side pre-check only — reject an obviously-doomed pick before ever
// opening a connection. The server's real ceiling is MAX_ATTACHMENT_BYTES
// (default 5GB, see bot/dashboard/server.py), far above this; this cap
// exists purely so a user isn't left waiting on a multi-gigabyte upload
// over mobile data without at least a moment's warning.
private const val WARN_ATTACHMENT_BYTES = 2L * 1024 * 1024 * 1024

class AttachmentTooLargeException : Exception("That file is over 2GB — sending it over a mobile connection isn't a good idea. Try Wi-Fi.")

@Singleton
class ChatRepository @Inject constructor(
    private val apiService: ApiService,
    private val dao: ChatDao,
    @ApplicationContext private val context: Context,
) {

    suspend fun recipients(): List<BotInstanceSummary> = apiService.chatRecipients().instances

    /** Room is the source of truth for what's shown — bounded to the most
     * recent rows so a long-lived conversation can't grow this screen's
     * memory usage forever the way the old plain in-memory list did. */
    fun observeMessages(instanceId: Int): Flow<List<ChatMessage>> =
        dao.observeRecent(instanceId).map { entities -> entities.map(::toDomain) }

    /** Fetches only what's new since Room's own last-seen id for this
     * instance (falling back to a full fetch the very first time), inserts
     * it, and prunes anything beyond the retained window — this replaces
     * the old panel.lastId bookkeeping the ViewModel used to carry in
     * memory. */
    suspend fun refreshMessages(instanceId: Int): Result<Unit> = runCatching {
        val afterId = dao.maxId(instanceId)
        val fresh = apiService.chatMessages(instanceId = instanceId, afterId = afterId)
        if (fresh.isNotEmpty()) {
            dao.insertAll(fresh.map { toEntity(instanceId, it) })
            dao.pruneOld(instanceId)
        }
    }

    private fun toEntity(instanceId: Int, m: ChatMessage) = ChatMessageEntity(
        instanceId = instanceId,
        id = m.id,
        ts = m.ts,
        platform = m.platform,
        chatId = m.chatId,
        userId = m.userId,
        username = m.username,
        direction = m.direction,
        source = m.source,
        text = m.text,
        attachmentPath = m.attachmentPath,
        attachmentName = m.attachmentName,
        attachmentMime = m.attachmentMime,
        attachmentSize = m.attachmentSize,
        thumbnailPath = m.thumbnailPath,
    )

    private fun toDomain(e: ChatMessageEntity) = ChatMessage(
        id = e.id,
        ts = e.ts,
        platform = e.platform,
        chatId = e.chatId,
        userId = e.userId,
        username = e.username,
        direction = e.direction,
        source = e.source,
        text = e.text,
        instanceId = e.instanceId,
        attachmentPath = e.attachmentPath,
        attachmentName = e.attachmentName,
        attachmentMime = e.attachmentMime,
        attachmentSize = e.attachmentSize,
        thumbnailPath = e.thumbnailPath,
    )

    suspend fun send(instanceId: Int, chatId: String, text: String) {
        apiService.sendMessage(SendMessageRequest(instanceId, chatId, text))
    }

    /** Chat with Bot mode — a real message TO the bot; the sender's identity
     * comes from this request's own auth (the paired device's api key), not
     * a client-declared chat_id. Returns the bot's real reply text. */
    suspend fun sendToBot(instanceId: Int, text: String): String? =
        apiService.sendToBot(SendToBotRequest(instanceId, text)).reply

    /** Streams GET /api/chat/attachments/{id} into cacheDir/downloads/ —
     * the pull-on-demand path (see bot/attachments.py): nothing is pushed
     * to the device until this is actually called. */
    suspend fun downloadAttachment(messageId: Int, suggestedName: String): File = withContext(Dispatchers.IO) {
        val body = apiService.downloadAttachment(messageId)
        val dir = File(context.cacheDir, "downloads").apply { mkdirs() }
        val safeName = suggestedName.ifBlank { "file" }.replace(Regex("[^A-Za-z0-9._-]+"), "_")
        val dest = File(dir, "${messageId}_$safeName")
        body.byteStream().use { input ->
            dest.outputStream().use { output -> input.copyTo(output) }
        }
        dest
    }

    /** Resolves the picked Uri's name/type via ContentResolver, then sends
     * it through the chunked-upload protocol (init, then one PUT per
     * chunk, then complete — see bot/dashboard/server.py's /api/uploads/
     * routes), reporting progress after each chunk. Copies to a cache file first so the
     * content:// stream (which may not support random access / re-reads)
     * becomes a plain file a RandomAccessFile can slice into fixed-size
     * chunks matching what init told the server to expect. */
    suspend fun sendFile(
        instanceId: Int,
        chatId: String,
        text: String,
        uri: Uri,
        onProgress: (Float) -> Unit = {},
    ) = withContext(Dispatchers.IO) {
        val resolver = context.contentResolver
        var name = "file"
        var declaredSize = -1L
        resolver.query(uri, null, null, null, null)?.use { cursor ->
            val nameIdx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            val sizeIdx = cursor.getColumnIndex(OpenableColumns.SIZE)
            if (cursor.moveToFirst()) {
                if (nameIdx >= 0) cursor.getString(nameIdx)?.let { name = it }
                if (sizeIdx >= 0) declaredSize = cursor.getLong(sizeIdx)
            }
        }
        if (declaredSize > WARN_ATTACHMENT_BYTES) throw AttachmentTooLargeException()

        val mime = resolver.getType(uri) ?: "application/octet-stream"
        val cacheFile = File(File(context.cacheDir, "uploads").apply { mkdirs() }, name)
        resolver.openInputStream(uri)!!.use { input ->
            cacheFile.outputStream().use { output -> input.copyTo(output) }
        }
        val totalSize = cacheFile.length()
        if (totalSize > WARN_ATTACHMENT_BYTES) {
            cacheFile.delete()
            throw AttachmentTooLargeException()
        }
        if (totalSize <= 0) {
            cacheFile.delete()
            throw IllegalArgumentException("That file is empty.")
        }

        try {
            val init = apiService.uploadInit(
                UploadInitRequest(instanceId = instanceId, chatId = chatId, filename = name, totalSize = totalSize, mime = mime, text = text)
            )
            RandomAccessFile(cacheFile, "r").use { raf ->
                var uploaded = 0L
                var index = 0
                val buf = ByteArray(init.chunkSize)
                while (uploaded < totalSize) {
                    val n = raf.read(buf)
                    if (n <= 0) break
                    val body = buf.copyOf(n).toRequestBody("application/octet-stream".toMediaType())
                    apiService.uploadChunk(init.sessionId, index, body)
                    uploaded += n
                    index += 1
                    onProgress((uploaded.toFloat() / totalSize.toFloat()).coerceIn(0f, 1f))
                }
            }
            apiService.uploadComplete(init.sessionId)
        } finally {
            cacheFile.delete()
        }
    }
}

package com.botserver.mobile.ui.chat

import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import com.botserver.mobile.data.dto.ChatMessage
import java.io.File

/** Plain-text export of a conversation — "save chats to device" per the
 * user's request, reusing the exact FileProvider share pattern
 * SettingsScreen's Diagnostics card already established for
 * AppLog.exportFile (build a real file under cacheDir, hand it out via
 * this app's FileProvider authority, launch a system share sheet) so a
 * user can save it into Drive/Files/email/etc. themselves rather than
 * this app guessing where "save to device" should mean. */
fun buildChatExportText(instanceName: String, messages: List<ChatMessage>): String = buildString {
    appendLine("BotServer chat export — $instanceName")
    appendLine("Exported ${messages.size} message(s)")
    appendLine("=".repeat(40))
    messages.forEach { m ->
        val who = when {
            m.direction == "out" -> "You"
            !m.username.isNullOrBlank() -> m.username
            else -> m.platform
        }
        appendLine()
        appendLine("[${m.ts}] $who:")
        if (m.text.isNotBlank()) appendLine(m.text)
        m.attachmentName?.let { appendLine("[attachment: $it]") }
    }
}

private fun safeFileNamePart(raw: String): String =
    raw.trim().ifEmpty { "chat" }.replace(Regex("[^A-Za-z0-9._-]"), "_").take(40)

fun exportChatFile(context: Context, instanceName: String, messages: List<ChatMessage>): File {
    val dir = File(context.cacheDir, "chat-exports").apply { mkdirs() }
    val file = File(dir, "${safeFileNamePart(instanceName)}-${System.currentTimeMillis()}.txt")
    file.writeText(buildChatExportText(instanceName, messages))
    return file
}

fun shareChatExportFile(context: Context, file: File) {
    val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
    val intent = Intent(Intent.ACTION_SEND).apply {
        type = "text/plain"
        putExtra(Intent.EXTRA_STREAM, uri)
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    }
    context.startActivity(Intent.createChooser(intent, "Export chat"))
}

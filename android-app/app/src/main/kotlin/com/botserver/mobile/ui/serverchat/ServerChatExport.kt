package com.botserver.mobile.ui.serverchat

import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import com.botserver.mobile.data.dto.ServerChatMessage
import java.io.File

/** Same plain-text-export-via-FileProvider-share pattern as
 * ui/chat/ChatExport.kt (bot Chat) and Settings' Diagnostics card —
 * "save this chat to device" means handing it to the system share sheet
 * so the user picks where it actually goes. Server Chat messages have
 * no username/platform field (see ServerChatMessage's own doc) — just a
 * sender device id — so [myDeviceId] is needed to label "You" correctly. */
fun buildServerChatExportText(title: String, messages: List<ServerChatMessage>, myDeviceId: Int): String = buildString {
    appendLine("BotServer Server Chat export — $title")
    appendLine("Exported ${messages.size} message(s)")
    appendLine("=".repeat(40))
    messages.forEach { m ->
        val who = if (m.senderDeviceId == myDeviceId) "You" else "Device #${m.senderDeviceId}"
        appendLine()
        appendLine("[${m.ts}] $who:")
        if (m.text.isNotBlank()) appendLine(m.text)
        m.attachmentName?.let { appendLine("[attachment: $it]") }
    }
}

private fun safeFileNamePart(raw: String): String =
    raw.trim().ifEmpty { "server-chat" }.replace(Regex("[^A-Za-z0-9._-]"), "_").take(40)

fun exportServerChatFile(context: Context, title: String, messages: List<ServerChatMessage>, myDeviceId: Int): File {
    val dir = File(context.cacheDir, "chat-exports").apply { mkdirs() }
    val file = File(dir, "${safeFileNamePart(title)}-${System.currentTimeMillis()}.txt")
    file.writeText(buildServerChatExportText(title, messages, myDeviceId))
    return file
}

fun shareServerChatExportFile(context: Context, file: File) {
    val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
    val intent = Intent(Intent.ACTION_SEND).apply {
        type = "text/plain"
        putExtra(Intent.EXTRA_STREAM, uri)
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    }
    context.startActivity(Intent.createChooser(intent, "Export chat"))
}

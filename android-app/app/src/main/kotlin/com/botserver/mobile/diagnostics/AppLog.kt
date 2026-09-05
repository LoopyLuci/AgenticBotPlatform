package com.botserver.mobile.diagnostics

import android.content.Context
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * A rolling, file-backed log independent of Logcat — Logcat disappears
 * the moment the app process dies or the phone reboots, which is exactly
 * when a "why didn't pairing work" report needs it most. Every entry
 * still goes to android.util.Log too, so `adb logcat` keeps working
 * unchanged; this is purely an additional, exportable copy that survives
 * a crash and works without a USB cable, so a report can come back from
 * a phone on a completely different network with no ADB access at all.
 *
 * File-based, not in-memory-only, for the same crash-survival reason —
 * see BotServerApp's uncaught-exception hook, which writes here before
 * the OS's own crash handling takes over, so a crash the instant Pair is
 * tapped still leaves a trail.
 */
object AppLog {
    private const val MAX_BYTES = 1_000_000L // ~1MB live file before rotating
    private const val FILE_NAME = "app.log"
    private const val ROTATED_NAME = "app.log.1"
    private val lock = ReentrantLock()
    private val timeFormat = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.US)

    @Volatile private var logDir: File? = null

    fun init(context: Context) {
        if (logDir != null) return
        logDir = File(context.filesDir, "logs").apply { mkdirs() }
    }

    fun d(tag: String, msg: String) {
        android.util.Log.d(tag, msg)
        write("D", tag, msg, null)
    }

    fun i(tag: String, msg: String) {
        android.util.Log.i(tag, msg)
        write("I", tag, msg, null)
    }

    fun w(tag: String, msg: String, t: Throwable? = null) {
        android.util.Log.w(tag, msg, t)
        write("W", tag, msg, t)
    }

    fun e(tag: String, msg: String, t: Throwable? = null) {
        android.util.Log.e(tag, msg, t)
        write("E", tag, msg, t)
    }

    private fun write(level: String, tag: String, msg: String, t: Throwable?) {
        val dir = logDir ?: return // init() not called yet — never crash the caller over a missing logger
        lock.withLock {
            try {
                val file = File(dir, FILE_NAME)
                rotateIfNeeded(file)
                file.appendText("${timeFormat.format(Date())} $level/$tag: $msg\n")
                if (t != null) file.appendText(t.stackTraceToString() + "\n")
            } catch (_: Exception) {
                // Logging must never itself crash the app, or throw a
                // second exception while already handling a first one.
            }
        }
    }

    private fun rotateIfNeeded(file: File) {
        if (file.length() < MAX_BYTES) return
        val rotated = File(file.parentFile, ROTATED_NAME)
        rotated.delete()
        file.renameTo(rotated)
    }

    /** Every currently-retained log line as one string — the rotated
     * backup first (older), then the live file, so it reads
     * chronologically. */
    fun readAll(context: Context): String {
        val dir = File(context.filesDir, "logs")
        val rotated = File(dir, ROTATED_NAME)
        val live = File(dir, FILE_NAME)
        val sb = StringBuilder()
        if (rotated.isFile) sb.append(rotated.readText())
        if (live.isFile) sb.append(live.readText())
        if (sb.isEmpty()) sb.append("(no log entries yet)")
        return sb.toString()
    }

    fun currentSizeBytes(context: Context): Long {
        val dir = File(context.filesDir, "logs")
        return listOf(File(dir, FILE_NAME), File(dir, ROTATED_NAME)).sumOf { if (it.isFile) it.length() else 0L }
    }

    /** A fresh, shareable snapshot file under cacheDir/logs/ (matching
     * file_paths.xml's "logs" cache-path) — a copy, not the live file
     * itself, so handing it to a share-sheet target can't race a
     * concurrent append from elsewhere in the app. */
    fun exportFile(context: Context): File {
        val dir = File(context.cacheDir, "logs").apply { mkdirs() }
        val out = File(dir, "botserver-diagnostics-${System.currentTimeMillis()}.txt")
        out.writeText(deviceHeader() + "\n" + readAll(context))
        return out
    }

    private fun deviceHeader(): String = buildString {
        appendLine("BotServer Android diagnostics export")
        appendLine("Generated: ${timeFormat.format(Date())}")
        appendLine("Device: ${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}, Android ${android.os.Build.VERSION.RELEASE} (SDK ${android.os.Build.VERSION.SDK_INT})")
        appendLine("----")
    }
}

package com.botserver.mobile.data

import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import com.botserver.mobile.data.dto.PendingApkResponse
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import javax.inject.Inject
import javax.inject.Singleton

/** Pull-based APK delivery from the desktop app's "Send APK"/"Send APK to
 * All Paired Devices" buttons — see bot/db.py's apk_pushes table comment
 * for why this is pull, not push: there's no reliable way to wake a
 * backgrounded phone without Firebase configured, so this device just
 * checks GET /api/android/apk/pending whenever it's convenient (the
 * Devices screen, on open) rather than waiting on a notification. */
@Singleton
class UpdateRepository @Inject constructor(
    private val apiService: ApiService,
    @ApplicationContext private val context: Context,
) {

    suspend fun checkPending(): PendingApkResponse = apiService.pendingApk()

    suspend fun downloadApk(pushId: Int): File = withContext(Dispatchers.IO) {
        val body = apiService.downloadApk(pushId)
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        val dest = File(dir, "BotServer.apk")
        body.byteStream().use { input ->
            dest.outputStream().use { output -> input.copyTo(output) }
        }
        dest
    }

    /** Same content:// + FileProvider pattern DevicesScreen's shareApk()
     * uses, but ACTION_VIEW straight at the system package installer
     * instead of a share sheet. Requires REQUEST_INSTALL_PACKAGES (see
     * AndroidManifest.xml) and, the first time, the user granting "install
     * unknown apps" for this app in Settings — the OS handles that prompt
     * itself when the intent fires without permission, no extra code needed
     * here beyond declaring the manifest permission. */
    fun installIntent(apk: File): Intent {
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", apk)
        return Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
    }
}

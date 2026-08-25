package com.botserver.mobile.data

import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import com.botserver.mobile.data.dto.ApkSendAllRequest
import com.botserver.mobile.data.dto.ApkSendRequest
import com.botserver.mobile.data.dto.MeshOrigin
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

    /** Offers *this* device's own installed APK to one other paired device
     * over the mesh — this phone's MeshServer (must be running; see
     * DevicesViewModel) will serve the actual bytes directly if the target
     * can reach it on the LAN. Falls back to nothing automatically: if the
     * target can't reach this device directly, its own pending-poll still
     * shows the push as available with mesh info, but the download will
     * fail — that's the honest limit of local-network-only transfer,
     * distinct from the desktop's server-relay send further below. */
    suspend fun sendTo(apiKeyId: Int): Int? = apiService.sendApk(ApkSendRequest(apiKeyId, mesh = true)).pushId

    suspend fun sendToAll(): Int = apiService.sendApkToAll(ApkSendAllRequest(mesh = true)).sentTo

    /** Tries a direct LAN connection to the origin device first (see
     * MeshServer.kt's connectToMeshPeer) when the pending push carries mesh
     * info, then falls back to the server-relay download — the "hybrid"
     * part: whichever path actually works, transparently to the caller. */
    suspend fun downloadApk(pushId: Int, mesh: MeshOrigin? = null): File = withContext(Dispatchers.IO) {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        val dest = File(dir, "BotServer.apk")
        if (mesh != null) {
            val direct = connectToMeshPeer(mesh.host, mesh.port, pushId, mesh.token, dest)
            if (direct != null) return@withContext direct
        }
        val body = apiService.downloadApk(pushId)
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

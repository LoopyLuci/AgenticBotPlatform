package com.botserver.mobile.data

import android.content.Context
import androidx.core.content.edit
import com.botserver.mobile.BuildConfig
import com.botserver.mobile.diagnostics.AppLog
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

data class GitHubRelease(val tag: String, val htmlUrl: String, val apkDownloadUrl: String, val apkName: String)

private const val REPO = "LoopyLuci/BotServer"
private const val LATEST_RELEASE_URL = "https://api.github.com/repos/$REPO/releases/latest"
private const val PREFS_NAME = "botserver_update_prefs"
private const val KEY_LAST_SEEN_TAG = "last_seen_release_tag"

/**
 * Self-update from the project's public GitHub releases (see the v0.5.0+
 * release published alongside this feature) — a source completely
 * independent of the paired BotServer connection, so it works even when
 * pairing itself is broken or the phone has never been paired at all.
 * Deliberately its own plain OkHttpClient rather than the shared,
 * paired-server-aware one from NetworkModule: GitHub is a fixed public
 * host, not the dynamic multi-candidate server DynamicHostInterceptor
 * exists to rewrite requests toward.
 *
 * There's no meaningful version-number comparison here (the git tags
 * this repo uses, e.g. "v0.5.0", track the desktop app's own Cargo.toml
 * version — a different numbering scheme than this app's own
 * versionName) — instead this just remembers the last release tag the
 * user has already seen/downloaded and flags anything newer, the same
 * "always offer the latest, let the person decide" philosophy the
 * existing server-push update flow (UpdateRepository) already uses.
 */
@Singleton
class GitHubUpdateRepository @Inject constructor(
    @ApplicationContext private val context: Context,
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .build()

    private val prefs get() = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    suspend fun checkLatest(): GitHubRelease? = withContext(Dispatchers.IO) {
        val request = Request.Builder().url(LATEST_RELEASE_URL).header("Accept", "application/vnd.github+json").build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                throw IOException("GitHub returned HTTP ${response.code} — it may be rate-limiting this network's IP, try again later")
            }
            val body = response.body?.string() ?: throw IOException("empty response from GitHub")
            val json = JSONObject(body)
            val tag = json.getString("tag_name")
            val htmlUrl = json.optString("html_url")
            val assets = json.optJSONArray("assets")
            var apkUrl: String? = null
            var apkName: String? = null
            if (assets != null) {
                for (i in 0 until assets.length()) {
                    val asset = assets.getJSONObject(i)
                    val name = asset.optString("name")
                    if (name.endsWith(".apk", ignoreCase = true)) {
                        apkUrl = asset.optString("browser_download_url")
                        apkName = name
                        break
                    }
                }
            }
            if (apkUrl.isNullOrBlank()) {
                AppLog.w("GitHubUpdate", "release $tag has no .apk asset attached")
                return@withContext null
            }
            AppLog.i("GitHubUpdate", "latest release is $tag (current app version ${BuildConfig.VERSION_NAME})")
            GitHubRelease(tag = tag, htmlUrl = htmlUrl, apkDownloadUrl = apkUrl, apkName = apkName ?: "app.apk")
        }
    }

    /** Whether `release` is worth showing as "an update is available" —
     * true whenever its tag differs from the last one this device has
     * already downloaded or explicitly dismissed. */
    fun isNew(release: GitHubRelease): Boolean = prefs.getString(KEY_LAST_SEEN_TAG, null) != release.tag

    fun markSeen(release: GitHubRelease) {
        prefs.edit { putString(KEY_LAST_SEEN_TAG, release.tag) }
    }

    suspend fun download(release: GitHubRelease): File = withContext(Dispatchers.IO) {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        val dest = File(dir, "BotServer-${release.tag}.apk")
        val request = Request.Builder().url(release.apkDownloadUrl).build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw IOException("download failed — HTTP ${response.code}")
            val body = response.body ?: throw IOException("empty download body")
            body.byteStream().use { input ->
                dest.outputStream().use { output -> input.copyTo(output) }
            }
        }
        AppLog.i("GitHubUpdate", "downloaded ${release.tag} (${dest.length()} bytes) to ${dest.name}")
        dest
    }
}

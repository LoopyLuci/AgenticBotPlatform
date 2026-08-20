package com.botserver.mobile.data

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Where the paired server's host and API key live — the mobile equivalent
 * of the web dashboard's `localStorage.getItem('dashboard_token')` (see
 * dashboard.html's getToken()/setToken()), except backed by the Android
 * Keystore via EncryptedSharedPreferences rather than plain storage, since
 * a phone leaves the house and a browser tab doesn't.
 */
@Singleton
class CredentialStore @Inject constructor(@ApplicationContext context: Context) {

    private val prefs = EncryptedSharedPreferences.create(
        context,
        "botserver_credentials",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    var host: String?
        get() = prefs.getString(KEY_HOST, null)
        set(value) = prefs.edit().putString(KEY_HOST, value).apply()

    /** An optional second, independent path to the same server (e.g. a
     * Tailscale hostname alongside a LAN IP) — see DynamicHostInterceptor,
     * which fails over to this when [host] stops answering. */
    var host2: String?
        get() = prefs.getString(KEY_HOST2, null)
        set(value) = prefs.edit().putString(KEY_HOST2, value).apply()

    /** Which slot ("host" or "host2") last actually answered a request —
     * so a dead primary isn't re-probed on every single call once a
     * fallback has proven itself. Defaults to "host". */
    var lastGoodHost: String
        get() = prefs.getString(KEY_LAST_GOOD, null) ?: SLOT_HOST
        set(value) = prefs.edit().putString(KEY_LAST_GOOD, value).apply()

    var apiKey: String?
        get() = prefs.getString(KEY_API_KEY, null)
        set(value) = prefs.edit().putString(KEY_API_KEY, value).apply()

    val isPaired: Boolean
        get() = !host.isNullOrBlank() && !apiKey.isNullOrBlank()

    fun clear() {
        prefs.edit().remove(KEY_HOST).remove(KEY_HOST2).remove(KEY_LAST_GOOD).remove(KEY_API_KEY).apply()
    }

    /** Normalizes user/QR-supplied host input into a full base URL — accepts
     * "host:port", "http://host:port", or a bare host (defaults to :8787,
     * BotServer's default dashboard port). */
    private fun normalize(raw: String?): String {
        val h = raw?.trim().orEmpty()
        return when {
            h.startsWith("http://") || h.startsWith("https://") -> h.trimEnd('/')
            h.contains(":") -> "http://$h"
            h.isNotEmpty() -> "http://$h:8787"
            else -> "http://127.0.0.1:8787"
        }
    }

    fun baseUrl(): String = normalize(host)
    fun baseUrl2(): String? = host2?.takeIf { it.isNotBlank() }?.let { normalize(it) }

    /** The host slot to try first — whichever last actually worked. */
    fun preferredBaseUrl(): String = if (lastGoodHost == SLOT_HOST2) (baseUrl2() ?: baseUrl()) else baseUrl()

    /** The other slot, to fail over to — null if there's no second host. */
    fun otherBaseUrl(): String? = if (lastGoodHost == SLOT_HOST2) baseUrl() else baseUrl2()

    fun markGood(url: String) {
        lastGoodHost = if (url == baseUrl2()) SLOT_HOST2 else SLOT_HOST
    }

    companion object {
        private const val KEY_HOST = "host"
        private const val KEY_HOST2 = "host2"
        private const val KEY_LAST_GOOD = "last_good_host"
        private const val KEY_API_KEY = "api_key"
        const val SLOT_HOST = "host"
        const val SLOT_HOST2 = "host2"
    }
}

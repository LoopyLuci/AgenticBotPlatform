package com.botserver.mobile.data

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Where the paired server's host(s) and API key live — the mobile
 * equivalent of the web dashboard's `localStorage.getItem('dashboard_token')`
 * (see dashboard.html's getToken()/setToken()), except backed by the
 * Android Keystore via EncryptedSharedPreferences rather than plain
 * storage, since a phone leaves the house and a browser tab doesn't.
 *
 * Up to three independent network paths to the same server are stored —
 * typically a LAN IP, a Tailscale IP, and a public Tailscale Funnel HTTPS
 * URL (see bot/network_info.py's detect_addresses()/detect_funnel_url()) —
 * so the app can reach the server from home Wi-Fi, from anywhere Tailscale
 * can connect, or from literally any network at all via Funnel, without
 * the user ever needing to know or pick which one applies right now.
 * DynamicHostInterceptor tries them in priority order automatically.
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

    /** A second, independent path to the same server (e.g. a Tailscale
     * hostname alongside a LAN IP) — see DynamicHostInterceptor, which
     * fails over to this when [host] stops answering. */
    var host2: String?
        get() = prefs.getString(KEY_HOST2, null)
        set(value) = prefs.edit().putString(KEY_HOST2, value).apply()

    /** A third, independent path — typically a Tailscale Funnel public
     * HTTPS URL, the one path that works from any network (cellular, a
     * stranger's Wi-Fi, behind a proxy) with no shared LAN or even
     * Tailscale connectivity required on the phone's end. Tried last
     * (see candidateUrls()) since it costs an extra relay hop. */
    var host3: String?
        get() = prefs.getString(KEY_HOST3, null)
        set(value) = prefs.edit().putString(KEY_HOST3, value).apply()

    /** Which slot ("host"/"host2"/"host3") last actually answered a
     * request — so a dead primary isn't re-probed on every single call
     * once a fallback has proven itself. Defaults to "host". */
    var lastGoodHost: String
        get() = prefs.getString(KEY_LAST_GOOD, null) ?: SLOT_HOST
        set(value) = prefs.edit().putString(KEY_LAST_GOOD, value).apply()

    var apiKey: String?
        get() = prefs.getString(KEY_API_KEY, null)
        set(value) = prefs.edit().putString(KEY_API_KEY, value).apply()

    val isPaired: Boolean
        get() = !host.isNullOrBlank() && !apiKey.isNullOrBlank()

    fun clear() {
        prefs.edit().remove(KEY_HOST).remove(KEY_HOST2).remove(KEY_HOST3).remove(KEY_LAST_GOOD).remove(KEY_API_KEY).apply()
    }

    /** Normalizes user/QR-supplied host input into a full base URL — accepts
     * "host:port", "http://host:port", "https://host" (e.g. a Funnel URL),
     * or a bare host (defaults to :8787, BotServer's default dashboard
     * port). */
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
    fun baseUrl3(): String? = host3?.takeIf { it.isNotBlank() }?.let { normalize(it) }

    private fun slotUrl(slot: String): String? = when (slot) {
        SLOT_HOST -> baseUrl()
        SLOT_HOST2 -> baseUrl2()
        SLOT_HOST3 -> baseUrl3()
        else -> null
    }

    /** Every configured host, in the order DynamicHostInterceptor should
     * try them: whichever slot last actually worked, first (so a request
     * doesn't keep re-probing a dead path once a fallback has proven
     * itself); then the remaining configured slots in fixed priority
     * order (LAN, then Tailscale, then Funnel last — Funnel adds a relay
     * hop, so it's the right thing to reach for only once the direct
     * paths have already failed). Never contains nulls or duplicates. */
    fun candidateUrls(): List<String> {
        val preferred = slotUrl(lastGoodHost)
        val rest = listOf(baseUrl(), baseUrl2(), baseUrl3())
        return (listOfNotNull(preferred) + rest.filterNotNull()).distinct()
    }

    fun markGood(url: String) {
        lastGoodHost = when (url) {
            baseUrl2() -> SLOT_HOST2
            baseUrl3() -> SLOT_HOST3
            else -> SLOT_HOST
        }
    }

    companion object {
        private const val KEY_HOST = "host"
        private const val KEY_HOST2 = "host2"
        private const val KEY_HOST3 = "host3"
        private const val KEY_LAST_GOOD = "last_good_host"
        private const val KEY_API_KEY = "api_key"
        const val SLOT_HOST = "host"
        const val SLOT_HOST2 = "host2"
        const val SLOT_HOST3 = "host3"
    }
}

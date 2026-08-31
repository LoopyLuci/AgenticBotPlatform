package com.botserver.mobile.data

import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.tasks.await
import java.net.URI
import java.net.URLDecoder
import javax.inject.Inject
import javax.inject.Singleton

data class PairingPayload(val host: String?, val host2: String? = null, val key: String)

@Singleton
class PairingRepository @Inject constructor(
    private val credentials: CredentialStore,
    private val apiService: ApiService,
    private val pushRepository: PushRepository,
) {
    /** Parses the botserver://pair?host=...&host2=...&key=... URI the
     * dashboard's Mobile tab QR encodes (see bot/dashboard/server.py's
     * api_mobile_keys_create()) — also accepts a bare "host|key" or just a
     * raw key string typed in manually, so a QR-less pairing flow works
     * from a copy-pasted key too. host2 is an optional second, independent
     * path to the same server (e.g. Tailscale alongside LAN) that
     * DynamicHostInterceptor fails over to automatically. */
    fun parse(raw: String): PairingPayload? {
        val trimmed = raw.trim()
        if (trimmed.startsWith("botserver://")) {
            val query = runCatching { URI(trimmed).query }.getOrNull() ?: return null
            val params = queryParams(query)
            val key = params["key"] ?: return null
            return PairingPayload(host = params["host"], host2 = params["host2"], key = key)
        }
        if (trimmed.isEmpty()) return null
        return PairingPayload(host = null, key = trimmed)
    }

    private fun queryParams(query: String): Map<String, String> =
        query.split("&").mapNotNull { part ->
            val idx = part.indexOf('=')
            if (idx < 0) return@mapNotNull null
            val name = part.substring(0, idx)
            val value = runCatching { URLDecoder.decode(part.substring(idx + 1), "UTF-8") }.getOrDefault("")
            name to value
        }.toMap()

    /** Stores the credential and confirms it actually works against the
     * real server before declaring pairing successful — a key that merely
     * *parses* isn't proof it's valid. Tries the primary host first, then
     * the fallback if given, so pairing itself proves whichever path
     * actually answers rather than assuming the first one is reachable. */
    suspend fun pairAndVerify(payload: PairingPayload, manualHost: String?, manualHost2: String? = null): Result<Unit> {
        val host = payload.host ?: manualHost
        val host2 = payload.host2 ?: manualHost2
        if (host.isNullOrBlank()) return Result.failure(IllegalArgumentException("A host is required — scan a QR that includes one, or enter it manually."))
        credentials.host = host
        credentials.host2 = host2
        credentials.apiKey = payload.key
        credentials.lastGoodHost = CredentialStore.SLOT_HOST

        suspend fun tryVerify(): Result<Unit> = runCatching { apiService.chatRecipients() }.map {}

        var result = tryVerify()
        if (result.isFailure && !host2.isNullOrBlank()) {
            credentials.lastGoodHost = CredentialStore.SLOT_HOST2
            result = tryVerify()
        }

        return result.fold(
            onSuccess = {
                // FCM tokens are typically issued at first app launch,
                // before pairing exists to send them to — register
                // whatever the current one is now that there's
                // somewhere to send it. Best-effort: a missing/invalid
                // google-services.json (see docs/mobile-access.md)
                // makes this fail harmlessly, it never blocks pairing.
                runCatching { FirebaseMessaging.getInstance().token.await() }
                    .onSuccess { token -> pushRepository.registerToken(token) }
                Result.success(Unit)
            },
            onFailure = { e ->
                credentials.clear()
                Result.failure(e)
            },
        )
    }
}

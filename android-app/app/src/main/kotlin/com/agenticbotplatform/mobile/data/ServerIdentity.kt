package com.agenticbotplatform.mobile.data

import java.util.concurrent.TimeUnit
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.Request

/**
 * Tells "my paired server, at a new address" apart from "some OTHER
 * AgenticBotPlatform on the network" before the app adopts an address.
 *
 * The app keeps several addresses for its server and refreshes them from the
 * server itself (HostSyncRepository) and from mDNS discovery
 * (DynamicHostInterceptor). Neither used to check WHICH server answered: a
 * second machine, a test instance, or a stale advertisement of an old one
 * could silently replace a working address — and the API key would then be
 * sent there. Every install serves a random, non-secret `server_id` in its
 * unauthenticated /healthz (bot/server_identity.py); the id learned at pairing
 * is the only one an address may report.
 *
 * Probes use their OWN plain client — no DynamicHostInterceptor (which would
 * rewrite the very address being tested), no auth header (nothing secret is
 * sent to a host that hasn't been verified yet), short timeouts.
 */
open class ServerIdentity(
    private val client: OkHttpClient = defaultProbeClient(),
) {
    sealed interface Probe {
        /** Nothing ABP-shaped answered (down, wrong port, not ABP, bad JSON). */
        data object Unreachable : Probe

        /** An ABP answered. [serverId] is null for a server too old to report one. */
        data class Abp(val serverId: String?) : Probe
    }

    /** Blocking — callers already run on IO threads (interceptor) or dispatch
     * there (repository). Never throws. */
    open fun probe(baseUrl: String): Probe {
        val request = runCatching { Request.Builder().url("${baseUrl.trimEnd('/')}/healthz").get().build() }
            .getOrNull() ?: return Probe.Unreachable
        // Same cleartext rule as every other request (see PrivateNetworkGuard):
        // never talk plain HTTP to an address outside the private/Tailscale
        // ranges, even just to ask who it is.
        if (request.url.scheme == "http" && !PrivateNetworkGuard.isAllowedHost(request.url.host)) return Probe.Unreachable
        return runCatching {
            client.newCall(request).execute().use { response ->
                // 503 still means "an ABP is here, its DB is unhappy" — identity is what we want.
                val body = response.body?.string().orEmpty()
                val json = Json.parseToJsonElement(body).jsonObject
                if (json["status"] == null) return@use Probe.Unreachable
                Probe.Abp(json.serverId())
            }
        }.getOrElse { Probe.Unreachable }
    }

    /**
     * May [baseUrl] be treated as the paired server? With a known [expectedId]
     * only an ABP reporting exactly that id qualifies. With none (a pairing
     * made before servers reported ids, or one that hasn't learned it yet) any
     * reachable ABP does — the previous behaviour, kept so those still work.
     */
    fun isPairedServer(baseUrl: String, expectedId: String?): Boolean =
        when (val p = probe(baseUrl)) {
            is Probe.Unreachable -> false
            is Probe.Abp -> expectedId == null || p.serverId == expectedId
        }

    private fun JsonObject.serverId(): String? =
        this["server_id"]?.jsonPrimitive?.contentOrNull?.takeIf { it.isNotBlank() }

    companion object {
        fun defaultProbeClient(): OkHttpClient =
            OkHttpClient.Builder()
                .connectTimeout(3, TimeUnit.SECONDS)
                .readTimeout(3, TimeUnit.SECONDS)
                .callTimeout(5, TimeUnit.SECONDS)
                .retryOnConnectionFailure(false)
                .build()
    }
}

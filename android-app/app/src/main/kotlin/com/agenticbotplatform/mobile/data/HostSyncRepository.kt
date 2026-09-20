package com.agenticbotplatform.mobile.data

import com.agenticbotplatform.mobile.diagnostics.AppLog
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

private const val TAG = "HostSync"

/**
 * Keeps a paired device's stored hosts (CredentialStore.host/host2/host3)
 * fresh against the server's own live-detected addresses, so a LAN IP
 * changing under DHCP, or Tailscale Funnel getting turned on/off later,
 * self-heals the next time the app is used instead of requiring a manual
 * re-pair — the whole point of "connect from anywhere without ever having
 * to think about it." Best-effort and silent: called opportunistically
 * (see HomeViewModel's init) whenever the app already has a working
 * connection, since GET /api/network-info itself needs to succeed through
 * DynamicHostInterceptor's own failover first — this can't fix a fully
 * dead pairing, only keep a working one from drifting stale.
 */
@Singleton
class HostSyncRepository @Inject constructor(
    private val apiService: ApiService,
    private val credentials: CredentialStore,
    private val identity: ServerIdentity = ServerIdentity(),
) {
    /** Fetches the server's current LAN/Tailscale/Funnel addresses and
     * writes any that changed into the corresponding slot (LAN → host,
     * Tailscale → host2, Funnel → host3 — matching the same convention
     * bot/dashboard/server.py's api_mobile_keys_create() auto-fill uses).
     *
     * A reported address is only ADOPTED once it has been checked: it must
     * answer /healthz as this same server (see ServerIdentity). Previously the
     * response was written straight over the stored hosts, so an address the
     * server merely believed it had — a LAN IP it isn't listening on (a server
     * paired over USB via `adb reverse`, bound to loopback), or a Funnel URL
     * that fronts a different instance — replaced the one that was actually
     * working, and the app lost its connection right after pairing.
     *
     * Never touches a slot the server reports as unavailable right now — a
     * momentary "Tailscale looks down" shouldn't erase a host that's still
     * worth retrying next time. Swallows every failure; this is a nice-to-have
     * background refresh, never a request the rest of the app should have to
     * handle an error from. */
    suspend fun syncHosts() {
        val info = runCatching { apiService.networkInfo() }.getOrNull() ?: return
        val expectedId = credentials.serverId ?: learnServerId()
        adopt("host", info.lan, credentials.host, expectedId) { credentials.host = it }
        adopt("host2", info.tailscale, credentials.host2, expectedId) { credentials.host2 = it }
        adopt("host3", info.funnel, credentials.host3, expectedId) { credentials.host3 = it }
    }

    /** A pairing made before servers reported ids learns it here, from the
     * address that just answered the request above. */
    private suspend fun learnServerId(): String? = withContext(Dispatchers.IO) {
        val base = credentials.candidateUrls().firstOrNull() ?: return@withContext null
        (identity.probe(base) as? ServerIdentity.Probe.Abp)?.serverId?.also { credentials.serverId = it }
    }

    private suspend fun adopt(slot: String, reported: String?, current: String?, expectedId: String?, write: (String) -> Unit) {
        if (reported.isNullOrBlank() || reported == current) return
        val verified = withContext(Dispatchers.IO) {
            identity.isPairedServer(credentials.normalizeHost(reported), expectedId)
        }
        if (verified) {
            write(reported)
        } else {
            AppLog.w(TAG, "not adopting $slot=$reported: it doesn't answer as this server (keeping ${current ?: "nothing"})")
        }
    }
}

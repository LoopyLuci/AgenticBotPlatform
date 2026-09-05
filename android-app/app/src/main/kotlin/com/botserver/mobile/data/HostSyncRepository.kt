package com.botserver.mobile.data

import javax.inject.Inject
import javax.inject.Singleton

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
) {
    /** Fetches the server's current LAN/Tailscale/Funnel addresses and
     * writes any that changed into the corresponding slot (LAN → host,
     * Tailscale → host2, Funnel → host3 — matching the same convention
     * bot/dashboard/server.py's api_mobile_keys_create() auto-fill uses).
     * Never touches a slot the server reports as unavailable right now —
     * a momentary "Tailscale looks down" on the server shouldn't erase a
     * host that's still worth retrying next time. Swallows every failure;
     * this is a nice-to-have background refresh, never a request the rest
     * of the app should have to handle an error from. */
    suspend fun syncHosts() {
        val info = runCatching { apiService.networkInfo() }.getOrNull() ?: return
        info.lan?.let { if (it != credentials.host) credentials.host = it }
        info.tailscale?.let { if (it != credentials.host2) credentials.host2 = it }
        info.funnel?.let { if (it != credentials.host3) credentials.host3 = it }
    }
}

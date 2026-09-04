package com.botserver.mobile.data

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.util.Log
import dagger.hilt.android.qualifiers.ApplicationContext
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import javax.inject.Inject
import javax.inject.Singleton

/** Finds a real, live BotServer instance on the local network via mDNS/
 * DNS-SD (Android's NsdManager) — the client half of bot/mdns_advertise.py's
 * `_botserver._tcp.local.` advertisement. This is the last-resort fallback
 * DynamicHostInterceptor reaches for only when BOTH of a device's paired
 * hosts stop answering (e.g. a DHCP lease changed the LAN IP, or a
 * Tailscale hostname stopped resolving) — it's what lets the app heal
 * itself back onto the LAN without the user re-pairing by hand, as long as
 * the phone and server are on the same local network right now.
 *
 * [discoverBlocking] blocks the calling thread for up to [timeoutMs] — safe
 * to call from an OkHttp interceptor (which already runs on a background
 * dispatcher thread), never from the main thread. Best-effort only: any
 * failure (no Wi-Fi, NSD unsupported, nothing found in time) returns null
 * rather than throwing, so a device with NSD trouble degrades to exactly
 * today's two-host failover, not a crash. */
@Singleton
class NsdDiscoveryClient @Inject constructor(@ApplicationContext private val context: Context) {

    // The non-deprecated resolveService()/NsdServiceInfo.hostAddresses overloads
    // need API 34 — this app's minSdk is 26, so the deprecated single-address
    // API is the correct choice here, not an oversight.
    @Suppress("DEPRECATION")
    fun discoverBlocking(timeoutMs: Long = 4000): String? {
        val nsdManager = try {
            context.getSystemService(Context.NSD_SERVICE) as? NsdManager ?: return null
        } catch (_: Exception) {
            return null
        }
        val wifiManager = try {
            context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
        } catch (_: Exception) {
            null
        }
        // mDNS is multicast UDP — some Wi-Fi chipsets silently drop
        // multicast frames to save power unless something on-device holds
        // this lock, which would otherwise make discovery fail unpredictably
        // depending on hardware rather than on whether a server is present.
        val multicastLock = wifiManager?.createMulticastLock("botserver-nsd")?.apply {
            setReferenceCounted(true)
            try { acquire() } catch (_: Exception) {}
        }

        val result = AtomicReference<String?>(null)
        val latch = CountDownLatch(1)
        var resolving = false

        val resolveListener = object : NsdManager.ResolveListener {
            override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                latch.countDown()
            }

            override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                val hostAddress = serviceInfo.host?.hostAddress
                if (!hostAddress.isNullOrBlank() && serviceInfo.port > 0) {
                    result.set("$hostAddress:${serviceInfo.port}")
                }
                latch.countDown()
            }
        }

        val discoveryListener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                // First match wins — a home install normally has exactly
                // one BotServer advertising, and NsdManager only allows one
                // resolveService() in flight at a time per manager anyway.
                if (resolving) return
                resolving = true
                try {
                    nsdManager.resolveService(serviceInfo, resolveListener)
                } catch (_: Exception) {
                    latch.countDown()
                }
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo) {}
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                latch.countDown()
            }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
        }

        return try {
            nsdManager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discoveryListener)
            latch.await(timeoutMs, TimeUnit.MILLISECONDS)
            try { nsdManager.stopServiceDiscovery(discoveryListener) } catch (_: Exception) {}
            result.get()
        } catch (e: Exception) {
            Log.w(TAG, "NSD discovery failed: ${e.message}")
            null
        } finally {
            try { multicastLock?.release() } catch (_: Exception) {}
        }
    }

    companion object {
        const val SERVICE_TYPE = "_botserver._tcp."
        private const val TAG = "NsdDiscoveryClient"
    }
}

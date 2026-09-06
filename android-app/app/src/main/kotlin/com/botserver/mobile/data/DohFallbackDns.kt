package com.botserver.mobile.data

import java.net.InetAddress
import java.net.UnknownHostException
import java.util.concurrent.TimeUnit
import okhttp3.Dns
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject

/**
 * Falls back to DNS-over-HTTPS when the device's own system DNS can't
 * resolve a hostname — seen in practice on some Fire OS devices (no
 * Google Play Services, and apparently sometimes no working system DNS
 * resolver either), surfacing as OkHttp's "Unable to resolve host
 * ...: No address associated with hostname" for even a plain,
 * definitely-real hostname like api.github.com.
 *
 * The DoH request itself is made straight to Cloudflare's resolver by
 * its literal IP address (1.1.1.1) rather than by hostname — the whole
 * point of a *fallback* resolver is that it must not itself depend on
 * the DNS resolution that's already known to be broken. This works
 * because Cloudflare's certificate for that service covers the IP
 * address itself as a SAN, so TLS validation succeeds even though the
 * request's SNI/Host is a bare IP, not a name.
 *
 * Used only by [GitHubUpdateRepository]'s own OkHttpClient (self-update
 * talks to a fixed set of public GitHub hosts) — the main app's shared
 * client resolves the paired server's own host/IP via
 * DynamicHostInterceptor, which is a completely different problem this
 * has no business touching.
 */
object DohFallbackDns : Dns {

    private val dohClient = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(4, TimeUnit.SECONDS)
        .build()

    override fun lookup(hostname: String): List<InetAddress> {
        try {
            return Dns.SYSTEM.lookup(hostname)
        } catch (systemFailure: UnknownHostException) {
            val viaDoh = resolveViaDoh(hostname)
            if (viaDoh.isNotEmpty()) return viaDoh
            throw systemFailure
        }
    }

    private fun resolveViaDoh(hostname: String): List<InetAddress> {
        return try {
            val request = Request.Builder()
                .url("https://1.1.1.1/dns-query?name=$hostname&type=A")
                .header("Accept", "application/dns-json")
                .build()
            dohClient.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return emptyList()
                val body = response.body?.string() ?: return emptyList()
                val answers = JSONObject(body).optJSONArray("Answer") ?: return emptyList()
                buildList {
                    for (i in 0 until answers.length()) {
                        val entry = answers.getJSONObject(i)
                        if (entry.optInt("type") == 1) { // A record
                            val ip = entry.optString("data")
                            if (ip.isNotBlank()) add(InetAddress.getByName(ip))
                        }
                    }
                }
            }
        } catch (_: Exception) {
            emptyList()
        }
    }
}

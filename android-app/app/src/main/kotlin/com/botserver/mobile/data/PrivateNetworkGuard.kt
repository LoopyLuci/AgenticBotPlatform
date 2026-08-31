package com.botserver.mobile.data

import java.net.InetAddress
import java.net.UnknownHostException

/**
 * BotServer has no TLS of its own by design (see docs/mobile-access.md) —
 * cleartext HTTP is only safe here because the intended transport is a
 * Tailscale tunnel or a trusted LAN, both already private. Android's
 * declarative network_security_config.xml can only allow-list exact
 * domains, not IP ranges — and this app's host is whatever the user pairs
 * to at runtime, unknown at build time — so a static config can't express
 * "cleartext is fine, but only to a private address." This object is the
 * actual enforcement point instead: DynamicHostInterceptor calls
 * [isAllowedHost] before sending any cleartext request, and refuses to
 * attach the auth token (or send the request at all) to a host that
 * doesn't resolve into one of these ranges.
 */
object PrivateNetworkGuard {
    private class Cidr(base: String, val prefixBits: Int) {
        val base: ByteArray = InetAddress.getByName(base).address
    }

    private val ALLOWED = listOf(
        Cidr("100.64.0.0", 10), // Tailscale CGNAT range
        Cidr("10.0.0.0", 8), // RFC1918
        Cidr("172.16.0.0", 12), // RFC1918
        Cidr("192.168.0.0", 16), // RFC1918
        Cidr("127.0.0.0", 8), // loopback — same-device/emulator testing
    )

    /** True if every address `host` resolves to falls within an allowed range. */
    fun isAllowedHost(host: String): Boolean {
        val addresses = try {
            InetAddress.getAllByName(host)
        } catch (e: UnknownHostException) {
            return false
        }
        if (addresses.isEmpty()) return false
        return addresses.all { matchesAny(it.address) }
    }

    private fun matchesAny(addr: ByteArray): Boolean =
        addr.size == 4 && ALLOWED.any { matches(addr, it) } // IPv6 not covered by these ranges — treated as untrusted

    private fun matches(addr: ByteArray, cidr: Cidr): Boolean {
        val fullBytes = cidr.prefixBits / 8
        val remainingBits = cidr.prefixBits % 8
        for (i in 0 until fullBytes) {
            if (addr[i] != cidr.base[i]) return false
        }
        if (remainingBits > 0) {
            val mask = (0xFF shl (8 - remainingBits)) and 0xFF
            if ((addr[fullBytes].toInt() and mask) != (cidr.base[fullBytes].toInt() and mask)) return false
        }
        return true
    }
}

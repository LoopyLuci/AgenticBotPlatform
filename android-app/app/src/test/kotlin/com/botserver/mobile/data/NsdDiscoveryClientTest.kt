package com.botserver.mobile.data

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import io.mockk.Runs
import io.mockk.every
import io.mockk.just
import io.mockk.mockk
import java.net.InetAddress
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** NsdDiscoveryClient's own callback-driven discover/resolve logic — the
 * real mechanics DynamicHostInterceptorTest's mocked NsdDiscoveryClient
 * deliberately doesn't exercise. NsdManager/NsdServiceInfo are mocked
 * directly rather than instantiated for real: this module's build has
 * testOptions.unitTests.isReturnDefaultValues on, so a real framework
 * object's getters would just return stub defaults (0/null) instead of
 * whatever a real device would report — mocking sidesteps that entirely,
 * matching how CredentialStore etc. are already mocked elsewhere. */
class NsdDiscoveryClientTest {

    private fun buildClient(nsdManager: NsdManager?, wifiManager: WifiManager? = null): NsdDiscoveryClient {
        val context = mockk<Context>(relaxed = true)
        every { context.getSystemService(Context.NSD_SERVICE) } returns nsdManager
        every { context.applicationContext } returns context
        every { context.getSystemService(Context.WIFI_SERVICE) } returns wifiManager
        return NsdDiscoveryClient(context)
    }

    @Test
    fun `returns the resolved host and port when a service is found and resolves successfully`() {
        val nsdManager = mockk<NsdManager>()
        val serviceInfo = mockk<NsdServiceInfo>(relaxed = true)
        every { serviceInfo.host } returns InetAddress.getByName("192.168.1.50")
        every { serviceInfo.port } returns 8787

        every { nsdManager.discoverServices(any<String>(), any<Int>(), any<NsdManager.DiscoveryListener>()) } answers {
            thirdArg<NsdManager.DiscoveryListener>().onServiceFound(serviceInfo)
        }
        every { nsdManager.resolveService(any(), any()) } answers {
            secondArg<NsdManager.ResolveListener>().onServiceResolved(serviceInfo)
        }
        every { nsdManager.stopServiceDiscovery(any()) } just Runs

        val result = buildClient(nsdManager).discoverBlocking(timeoutMs = 500)

        assertEquals("192.168.1.50:8787", result)
    }

    @Test
    fun `returns null when the resolve step fails`() {
        val nsdManager = mockk<NsdManager>()
        val serviceInfo = mockk<NsdServiceInfo>(relaxed = true)

        every { nsdManager.discoverServices(any<String>(), any<Int>(), any<NsdManager.DiscoveryListener>()) } answers {
            thirdArg<NsdManager.DiscoveryListener>().onServiceFound(serviceInfo)
        }
        every { nsdManager.resolveService(any(), any()) } answers {
            secondArg<NsdManager.ResolveListener>().onResolveFailed(serviceInfo, 0)
        }
        every { nsdManager.stopServiceDiscovery(any()) } just Runs

        assertNull(buildClient(nsdManager).discoverBlocking(timeoutMs = 500))
    }

    @Test
    fun `returns null when starting discovery itself fails`() {
        val nsdManager = mockk<NsdManager>()
        every { nsdManager.discoverServices(any<String>(), any<Int>(), any<NsdManager.DiscoveryListener>()) } answers {
            thirdArg<NsdManager.DiscoveryListener>().onStartDiscoveryFailed(NsdDiscoveryClient.SERVICE_TYPE, 0)
        }
        every { nsdManager.stopServiceDiscovery(any()) } just Runs

        assertNull(buildClient(nsdManager).discoverBlocking(timeoutMs = 500))
    }

    @Test
    fun `returns null when nothing is found before the timeout elapses`() {
        val nsdManager = mockk<NsdManager>()
        // discoverServices "succeeds" but never calls back — simulates a
        // real network with no BotServer currently advertising.
        every { nsdManager.discoverServices(any<String>(), any<Int>(), any<NsdManager.DiscoveryListener>()) } just Runs
        every { nsdManager.stopServiceDiscovery(any()) } just Runs

        assertNull(buildClient(nsdManager).discoverBlocking(timeoutMs = 150))
    }

    @Test
    fun `returns null without touching NsdManager when NSD service is unavailable`() {
        assertNull(buildClient(nsdManager = null).discoverBlocking(timeoutMs = 150))
    }

    @Test
    fun `a resolved service with no host address is treated as not found`() {
        val nsdManager = mockk<NsdManager>()
        val serviceInfo = mockk<NsdServiceInfo>(relaxed = true)
        every { serviceInfo.host } returns null

        every { nsdManager.discoverServices(any<String>(), any<Int>(), any<NsdManager.DiscoveryListener>()) } answers {
            thirdArg<NsdManager.DiscoveryListener>().onServiceFound(serviceInfo)
        }
        every { nsdManager.resolveService(any(), any()) } answers {
            secondArg<NsdManager.ResolveListener>().onServiceResolved(serviceInfo)
        }
        every { nsdManager.stopServiceDiscovery(any()) } just Runs

        assertNull(buildClient(nsdManager).discoverBlocking(timeoutMs = 500))
    }

    @Test
    fun `ignores a second service found while a resolve is already in flight`() {
        val nsdManager = mockk<NsdManager>()
        val serviceInfo1 = mockk<NsdServiceInfo>(relaxed = true)
        val serviceInfo2 = mockk<NsdServiceInfo>(relaxed = true)
        every { serviceInfo1.host } returns InetAddress.getByName("192.168.1.50")
        every { serviceInfo1.port } returns 8787
        var resolveCalls = 0

        every { nsdManager.discoverServices(any<String>(), any<Int>(), any<NsdManager.DiscoveryListener>()) } answers {
            val listener = thirdArg<NsdManager.DiscoveryListener>()
            listener.onServiceFound(serviceInfo1)
            listener.onServiceFound(serviceInfo2) // a home network normally has exactly one — this should be ignored
        }
        every { nsdManager.resolveService(any(), any()) } answers {
            resolveCalls++
            secondArg<NsdManager.ResolveListener>().onServiceResolved(serviceInfo1)
        }
        every { nsdManager.stopServiceDiscovery(any()) } just Runs

        val result = buildClient(nsdManager).discoverBlocking(timeoutMs = 500)

        assertEquals(1, resolveCalls)
        assertEquals("192.168.1.50:8787", result)
    }
}

package com.botserver.mobile.di

import com.botserver.mobile.data.CredentialStore
import com.botserver.mobile.data.NsdDiscoveryClient
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import java.io.IOException
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Before
import org.junit.Test

/** DynamicHostInterceptor's host-rewrite + failover logic (di/NetworkModule.kt)
 * — the single point every request goes through to reach whichever of a
 * paired device's two hosts last worked, and to fall back to the other one
 * on a transport-level failure. Driven directly against real MockWebServer
 * instances rather than a live BotServer, and against literal IP hosts so
 * PrivateNetworkGuard's allow/deny check is exercised deterministically
 * with no real DNS lookups (a public IP literal like 8.8.8.8 is resolved
 * locally by InetAddress without any network call). */
class DynamicHostInterceptorTest {
    private lateinit var primary: MockWebServer
    private lateinit var secondary: MockWebServer
    private lateinit var credentials: CredentialStore
    private lateinit var nsdDiscovery: NsdDiscoveryClient
    private lateinit var client: OkHttpClient

    @Before
    fun setUp() {
        primary = MockWebServer().apply { start() }
        secondary = MockWebServer().apply { start() }
        credentials = mockk(relaxed = true)
        // Relaxed → discoverBlocking() returns null unless a test overrides
        // it, i.e. "nothing found on the network", same as real behavior
        // with NSD unsupported/no server advertising.
        nsdDiscovery = mockk(relaxed = true)
        client = OkHttpClient.Builder().addInterceptor(DynamicHostInterceptor(credentials, nsdDiscovery)).build()
    }

    @After
    fun tearDown() {
        primary.shutdown()
        secondary.shutdown()
    }

    private fun request(): Request = Request.Builder().url(PLACEHOLDER_BASE_URL).build()

    @Test
    fun `a successful primary request is not retried and marks the primary host good`() {
        val primaryUrl = primary.url("/").toString().trimEnd('/')
        every { credentials.preferredBaseUrl() } returns primaryUrl
        primary.enqueue(MockResponse().setResponseCode(200).setBody("ok"))

        val response = client.newCall(request()).execute()

        assertEquals(200, response.code)
        assertEquals(1, primary.requestCount)
        verify { credentials.markGood(primaryUrl) }
    }

    @Test
    fun `a primary connection failure falls back to the secondary host`() {
        val primaryUrl = "http://127.0.0.1:1" // nothing listens here — a real, immediate connection failure
        val secondaryUrl = secondary.url("/").toString().trimEnd('/')
        every { credentials.preferredBaseUrl() } returns primaryUrl
        every { credentials.otherBaseUrl() } returns secondaryUrl
        secondary.enqueue(MockResponse().setResponseCode(200).setBody("ok"))

        val response = client.newCall(request()).execute()

        assertEquals(200, response.code)
        assertEquals(1, secondary.requestCount)
        verify { credentials.markGood(secondaryUrl) }
    }

    @Test
    fun `both hosts failing propagates the original failure`() {
        every { credentials.preferredBaseUrl() } returns "http://127.0.0.1:1"
        every { credentials.otherBaseUrl() } returns "http://127.0.0.1:2"

        assertThrows(IOException::class.java) { client.newCall(request()).execute() }
    }

    @Test
    fun `no second host configured propagates the primary failure directly`() {
        every { credentials.preferredBaseUrl() } returns "http://127.0.0.1:1"
        every { credentials.otherBaseUrl() } returns null

        assertThrows(IOException::class.java) { client.newCall(request()).execute() }
    }

    @Test
    fun `a cleartext request to a public non-private host is refused and falls back`() {
        // 8.8.8.8 is outside every range PrivateNetworkGuard allows —
        // refused before ever attempting a connection, then falls back to
        // the secondary exactly like an unreachable host would.
        val secondaryUrl = secondary.url("/").toString().trimEnd('/')
        every { credentials.preferredBaseUrl() } returns "http://8.8.8.8:8787"
        every { credentials.otherBaseUrl() } returns secondaryUrl
        secondary.enqueue(MockResponse().setResponseCode(200).setBody("ok"))

        val response = client.newCall(request()).execute()

        assertEquals(200, response.code)
        verify { credentials.markGood(secondaryUrl) }
    }

    @Test
    fun `both hosts failing falls back to NSD discovery and persists the discovered host`() {
        val discoveredUrl = secondary.url("/").toString().trimEnd('/')
        val discoveredHostPort = discoveredUrl.removePrefix("http://")
        every { credentials.preferredBaseUrl() } returns "http://127.0.0.1:1"
        every { credentials.otherBaseUrl() } returns "http://127.0.0.1:2"
        every { credentials.lastGoodHost } returns CredentialStore.SLOT_HOST
        every { nsdDiscovery.discoverBlocking() } returns discoveredHostPort
        secondary.enqueue(MockResponse().setResponseCode(200).setBody("ok"))

        val response = client.newCall(request()).execute()

        assertEquals(200, response.code)
        assertEquals(1, secondary.requestCount)
        // The primary slot ("host") was the one marked good before this
        // call, so the newly discovered address overwrites the OTHER slot
        // ("host2") rather than clobbering a primary that might come back.
        verify { credentials.host2 = discoveredHostPort }
        verify { credentials.markGood(discoveredUrl) }
    }

    @Test
    fun `NSD discovery finding nothing propagates the original failure`() {
        every { credentials.preferredBaseUrl() } returns "http://127.0.0.1:1"
        every { credentials.otherBaseUrl() } returns "http://127.0.0.1:2"
        every { nsdDiscovery.discoverBlocking() } returns null

        assertThrows(IOException::class.java) { client.newCall(request()).execute() }
    }

    @Test
    fun `the auth header is attached from the credential store, not the request itself`() {
        val primaryUrl = primary.url("/").toString().trimEnd('/')
        every { credentials.preferredBaseUrl() } returns primaryUrl
        every { credentials.apiKey } returns "secret-token"
        primary.enqueue(MockResponse().setResponseCode(200))

        client.newCall(request()).execute()

        val recorded = primary.takeRequest()
        assertEquals("secret-token", recorded.getHeader("X-Dashboard-Token"))
    }
}

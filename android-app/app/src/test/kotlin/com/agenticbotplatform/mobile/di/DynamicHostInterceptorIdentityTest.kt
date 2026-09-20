package com.agenticbotplatform.mobile.di

import com.agenticbotplatform.mobile.data.CredentialStore
import com.agenticbotplatform.mobile.data.NsdDiscoveryClient
import com.agenticbotplatform.mobile.data.ServerIdentity
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

/** The interceptor's identity check on mDNS-discovered hosts and its fail-fast
 * breaker (the parts DynamicHostInterceptorTest doesn't cover). */
class DynamicHostInterceptorIdentityTest {
    private lateinit var found: MockWebServer
    private lateinit var credentials: CredentialStore
    private lateinit var nsd: NsdDiscoveryClient
    private lateinit var identity: ServerIdentity
    private var now = 1_000L
    private lateinit var client: OkHttpClient

    @Before
    fun setUp() {
        found = MockWebServer().apply { start() }
        credentials = mockk(relaxed = true)
        nsd = mockk(relaxed = true)
        identity = mockk()
        every { credentials.candidateUrls() } returns listOf("http://127.0.0.1:1")
        every { credentials.apiKey } returns "unused"
        client = OkHttpClient.Builder()
            .addInterceptor(DynamicHostInterceptor(credentials, nsd, identity) { now })
            .build()
    }

    @After
    fun tearDown() {
        found.shutdown()
    }

    private fun request() = Request.Builder().url(PLACEHOLDER_BASE_URL).build()
    private fun foundHostPort() = found.url("/").toString().removePrefix("http://").trimEnd('/')

    @Test
    fun `an mDNS host that is a different server is ignored and receives no request`() {
        every { credentials.serverId } returns "paired-id"
        every { nsd.discoverBlocking() } returns foundHostPort()
        every { identity.isPairedServer(any(), "paired-id") } returns false

        assertThrows(IOException::class.java) { client.newCall(request()).execute() }

        assertEquals(0, found.requestCount)
        verify(exactly = 0) { credentials.host = any() }
    }

    @Test
    fun `an mDNS host that is the paired server is adopted`() {
        every { credentials.serverId } returns "paired-id"
        every { nsd.discoverBlocking() } returns foundHostPort()
        every { identity.isPairedServer(any(), "paired-id") } returns true
        found.enqueue(MockResponse().setResponseCode(200).setBody("ok"))

        assertEquals(200, client.newCall(request()).execute().code)
        verify { credentials.host = foundHostPort() }
    }

    @Test
    fun `after every address fails, the next requests fail immediately for a short window`() {
        every { credentials.serverId } returns null
        every { nsd.discoverBlocking() } returns null

        assertThrows(IOException::class.java) { client.newCall(request()).execute() }
        verify(exactly = 1) { nsd.discoverBlocking() }

        now += 1_000
        assertThrows(IOException::class.java) { client.newCall(request()).execute() }
        verify(exactly = 1) { nsd.discoverBlocking() } // no second failover cycle
    }

    @Test
    fun `the breaker expires and the addresses are tried again`() {
        every { credentials.serverId } returns null
        every { nsd.discoverBlocking() } returns null

        assertThrows(IOException::class.java) { client.newCall(request()).execute() }
        now += 60_000
        assertThrows(IOException::class.java) { client.newCall(request()).execute() }

        verify(exactly = 2) { nsd.discoverBlocking() }
    }

    @Test
    fun `re-pairing to different addresses is not held back by an earlier failure`() {
        every { credentials.serverId } returns null
        every { nsd.discoverBlocking() } returns null
        assertThrows(IOException::class.java) { client.newCall(request()).execute() }

        val good = MockWebServer().apply { start() }
        try {
            every { credentials.candidateUrls() } returns listOf(good.url("/").toString().trimEnd('/'))
            good.enqueue(MockResponse().setResponseCode(200).setBody("ok"))
            assertEquals(200, client.newCall(request()).execute().code)
        } finally {
            good.shutdown()
        }
    }
}

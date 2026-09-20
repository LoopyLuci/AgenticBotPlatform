package com.agenticbotplatform.mobile.data

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/** ServerIdentity: telling "my paired server" from "some other ABP" via the
 * unauthenticated /healthz, against a real MockWebServer. */
class ServerIdentityTest {
    private lateinit var server: MockWebServer
    private lateinit var url: String
    private val identity = ServerIdentity()

    @Before
    fun setUp() {
        server = MockWebServer().apply { start() }
        url = server.url("/").toString().trimEnd('/')
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun health(body: String, code: Int = 200) =
        server.enqueue(MockResponse().setResponseCode(code).setBody(body))

    @Test
    fun `an ABP reporting an id is read back`() {
        health("""{"status":"ok","server_id":"abc123"}""")
        assertEquals(ServerIdentity.Probe.Abp("abc123"), identity.probe(url))
    }

    @Test
    fun `an older server with no id is still recognised as an ABP`() {
        health("""{"status":"ok"}""")
        assertEquals(ServerIdentity.Probe.Abp(null), identity.probe(url))
    }

    @Test
    fun `a 503 with a health body still identifies the server`() {
        health("""{"status":"degraded","server_id":"abc123"}""", code = 503)
        assertEquals(ServerIdentity.Probe.Abp("abc123"), identity.probe(url))
    }

    @Test
    fun `something that is not an ABP is unreachable`() {
        health("<html>router login</html>")
        assertEquals(ServerIdentity.Probe.Unreachable, identity.probe(url))
    }

    @Test
    fun `nothing listening is unreachable and never throws`() {
        assertEquals(ServerIdentity.Probe.Unreachable, identity.probe("http://127.0.0.1:1"))
    }

    @Test
    fun `the probe sends no credentials`() {
        health("""{"status":"ok","server_id":"abc123"}""")
        identity.probe(url)
        val recorded = server.takeRequest()
        assertEquals("/healthz", recorded.path)
        assertEquals(null, recorded.getHeader("X-Dashboard-Token"))
    }

    @Test
    fun `a cleartext probe to a public host is refused without connecting`() {
        assertEquals(ServerIdentity.Probe.Unreachable, identity.probe("http://8.8.8.8:8787"))
    }

    @Test
    fun `only the paired id qualifies when one is known`() {
        health("""{"status":"ok","server_id":"abc123"}""")
        health("""{"status":"ok","server_id":"someone-else"}""")
        assertTrue(identity.isPairedServer(url, "abc123"))
        assertFalse(identity.isPairedServer(url, "abc123"))
    }

    @Test
    fun `with no known id any reachable ABP qualifies`() {
        health("""{"status":"ok","server_id":"abc123"}""")
        assertTrue(identity.isPairedServer(url, null))
    }

    @Test
    fun `an unreachable address never qualifies`() {
        assertFalse(identity.isPairedServer("http://127.0.0.1:1", null))
    }
}

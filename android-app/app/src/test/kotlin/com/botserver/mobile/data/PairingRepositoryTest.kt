package com.botserver.mobile.data

import io.mockk.mockk
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** PairingRepository.parse() — the one function standing between "scan a
 * QR" / "paste a link" / "type a bare key" and a working pairing attempt.
 * Table-driven over every input shape the class doc claims to accept. */
class PairingRepositoryTest {
    private val repository = PairingRepository(mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true))

    @Test
    fun `a full pairing URI with both hosts parses every field`() {
        val result = repository.parse("botserver://pair?host=192.168.1.5&host2=my-tailnet.ts.net&key=abc123")

        assertEquals(PairingPayload(host = "192.168.1.5", host2 = "my-tailnet.ts.net", key = "abc123"), result)
    }

    @Test
    fun `a pairing URI with all three hosts parses host3 (the Funnel URL) too`() {
        val encoded = "botserver://pair?host=192.168.1.5&host2=100.101.98.77%3A8787" +
            "&host3=https%3A%2F%2Fshivati.example.ts.net&key=abc123"
        val result = repository.parse(encoded)

        assertEquals(
            PairingPayload(host = "192.168.1.5", host2 = "100.101.98.77:8787", host3 = "https://shivati.example.ts.net", key = "abc123"),
            result,
        )
    }

    @Test
    fun `a pairing URI with only a host and key parses host2 as null`() {
        val result = repository.parse("botserver://pair?host=192.168.1.5&key=abc123")

        assertEquals(PairingPayload(host = "192.168.1.5", host2 = null, key = "abc123"), result)
    }

    @Test
    fun `a pairing URI missing the key entirely fails to parse`() {
        val result = repository.parse("botserver://pair?host=192.168.1.5")

        assertNull(result)
    }

    @Test
    fun `a bare typed key with no URI scheme is treated as the key alone`() {
        val result = repository.parse("just-a-raw-key")

        assertEquals(PairingPayload(host = null, host2 = null, key = "just-a-raw-key"), result)
    }

    @Test
    fun `surrounding whitespace from a copy-paste is trimmed`() {
        val result = repository.parse("  just-a-raw-key  \n")

        assertEquals(PairingPayload(host = null, host2 = null, key = "just-a-raw-key"), result)
    }

    @Test
    fun `an empty string fails to parse`() {
        assertNull(repository.parse(""))
        assertNull(repository.parse("   "))
    }

    @Test
    fun `a percent-encoded host in the query string is decoded`() {
        val result = repository.parse("botserver://pair?host=my%20server&key=abc123")

        assertEquals("my server", result?.host)
    }
}

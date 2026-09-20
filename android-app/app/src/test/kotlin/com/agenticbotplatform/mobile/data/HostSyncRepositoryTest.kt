package com.agenticbotplatform.mobile.data

import com.agenticbotplatform.mobile.data.dto.NetworkInfoResponse
import io.mockk.coEvery
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import kotlinx.coroutines.runBlocking
import org.junit.Before
import org.junit.Test

/** HostSyncRepository only adopts server-reported addresses that verify as the
 * paired server — a working (e.g. USB/loopback) host must never be overwritten
 * by an address that doesn't answer. */
class HostSyncRepositoryTest {
    private lateinit var api: ApiService
    private lateinit var credentials: CredentialStore
    private lateinit var identity: ServerIdentity
    private lateinit var repo: HostSyncRepository

    @Before
    fun setUp() {
        api = mockk()
        credentials = mockk(relaxed = true)
        identity = mockk()
        every { credentials.host } returns "127.0.0.1:8790"
        every { credentials.host2 } returns null
        every { credentials.host3 } returns null
        every { credentials.serverId } returns "paired-id"
        every { credentials.normalizeHost(any()) } answers { "http://" + firstArg<String>() }
        repo = HostSyncRepository(api, credentials, identity)
    }

    @Test
    fun `a reported LAN address that does not answer as the paired server is not adopted`() = runBlocking {
        coEvery { api.networkInfo() } returns NetworkInfoResponse(lan = "192.168.69.145:8791")
        every { identity.isPairedServer("http://192.168.69.145:8791", "paired-id") } returns false

        repo.syncHosts()

        verify(exactly = 0) { credentials.host = any() }
    }

    @Test
    fun `a verified address is adopted into its slot`() = runBlocking {
        coEvery { api.networkInfo() } returns NetworkInfoResponse(lan = "192.168.1.5:8787", tailscale = "100.1.2.3:8787")
        every { identity.isPairedServer(any(), "paired-id") } returns true

        repo.syncHosts()

        verify { credentials.host = "192.168.1.5:8787" }
        verify { credentials.host2 = "100.1.2.3:8787" }
    }

    @Test
    fun `an unchanged address is not re-probed`() = runBlocking {
        coEvery { api.networkInfo() } returns NetworkInfoResponse(lan = "127.0.0.1:8790")

        repo.syncHosts()

        verify(exactly = 0) { identity.isPairedServer(any(), any()) }
    }

    @Test
    fun `a failed network-info call changes nothing`() = runBlocking {
        coEvery { api.networkInfo() } throws java.io.IOException("down")

        repo.syncHosts()

        verify(exactly = 0) { credentials.host = any() }
        verify(exactly = 0) { credentials.host2 = any() }
    }

    @Test
    fun `a pairing with no stored id learns it from the answering server`() = runBlocking {
        every { credentials.serverId } returns null
        every { credentials.candidateUrls() } returns listOf("http://127.0.0.1:8790")
        every { identity.probe("http://127.0.0.1:8790") } returns ServerIdentity.Probe.Abp("learned")
        coEvery { api.networkInfo() } returns NetworkInfoResponse()

        repo.syncHosts()

        verify { credentials.serverId = "learned" }
    }
}

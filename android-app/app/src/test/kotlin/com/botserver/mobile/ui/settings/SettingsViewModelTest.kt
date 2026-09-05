package com.botserver.mobile.ui.settings

import com.botserver.mobile.data.CredentialStore
import com.botserver.mobile.data.GitHubUpdateRepository
import com.botserver.mobile.data.SettingsRepository
import com.botserver.mobile.data.UpdateRepository
import io.mockk.mockk
import io.mockk.verify
import org.junit.Test

/** SettingsViewModel.forgetPairing() — the "Clear app settings" button's
 * only real behavior: wiping CredentialStore, purely locally, with no
 * network call (the pairing key itself stays valid server-side until the
 * operator revokes it from the dashboard — see the function's own doc).
 * Synchronous, so no coroutine dispatcher rule is needed to test it. */
class SettingsViewModelTest {

    private fun buildViewModel(
        repository: SettingsRepository = mockk(relaxed = true),
        credentials: CredentialStore = mockk(relaxed = true),
        gitHubUpdateRepository: GitHubUpdateRepository = mockk(relaxed = true),
        updateRepository: UpdateRepository = mockk(relaxed = true),
    ) = SettingsViewModel(repository, credentials, gitHubUpdateRepository, updateRepository)

    @Test
    fun `forgetPairing clears the credential store and nothing else`() {
        val credentials = mockk<CredentialStore>(relaxed = true)
        val viewModel = buildViewModel(credentials = credentials)

        viewModel.forgetPairing()

        verify(exactly = 1) { credentials.clear() }
    }
}

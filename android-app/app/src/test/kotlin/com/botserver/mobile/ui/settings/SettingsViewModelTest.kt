package com.botserver.mobile.ui.settings

import com.botserver.mobile.data.CredentialStore
import com.botserver.mobile.data.SettingsRepository
import io.mockk.mockk
import io.mockk.verify
import org.junit.Test

/** SettingsViewModel.forgetPairing() — the "Clear app settings" button's
 * only real behavior: wiping CredentialStore, purely locally, with no
 * network call (the pairing key itself stays valid server-side until the
 * operator revokes it from the dashboard — see the function's own doc).
 * Synchronous, so no coroutine dispatcher rule is needed to test it. */
class SettingsViewModelTest {

    @Test
    fun `forgetPairing clears the credential store and nothing else`() {
        val repository = mockk<SettingsRepository>(relaxed = true)
        val credentials = mockk<CredentialStore>(relaxed = true)
        val viewModel = SettingsViewModel(repository, credentials)

        viewModel.forgetPairing()

        verify(exactly = 1) { credentials.clear() }
    }
}

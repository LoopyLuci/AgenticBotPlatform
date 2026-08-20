package com.botserver.mobile.ui.pairing

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.CredentialStore
import com.botserver.mobile.data.PairingPayload
import com.botserver.mobile.data.PairingRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

sealed interface PairingState {
    data object Idle : PairingState
    data object Verifying : PairingState
    data object Success : PairingState
    data class Error(val message: String) : PairingState
}

@HiltViewModel
class PairingViewModel @Inject constructor(
    private val repository: PairingRepository,
    private val credentials: CredentialStore,
) : ViewModel() {

    private val _state = MutableStateFlow<PairingState>(PairingState.Idle)
    val state: StateFlow<PairingState> = _state

    /** Called with whatever a scanned QR's raw text was. */
    fun onScanned(raw: String) {
        val payload = repository.parse(raw) ?: run {
            _state.value = PairingState.Error("That QR doesn't look like a Bot Server pairing code.")
            return
        }
        attemptPair(payload.host, payload.key, payload.host2)
    }

    /** Called once, at most, with the activity's launch intent data — a
     * botserver://pair link tapped from a share or fired by the desktop
     * app's adb auto-pair flow. Reuses the exact same parse+verify path a
     * QR scan does; a malformed/incomplete link falls through silently to
     * the normal scan/manual-entry screen rather than showing an error for
     * a link the user never deliberately triggered. */
    fun onAutoPairLink(raw: String) {
        val payload = repository.parse(raw) ?: return
        if (payload.host.isNullOrBlank()) return
        attemptPair(payload.host, payload.key, payload.host2)
    }

    fun onManualSubmit(host: String, key: String, host2: String = "") {
        val trimmedKey = key.trim()
        if (trimmedKey.isEmpty()) {
            _state.value = PairingState.Error("Paste the key from the dashboard's Mobile tab.")
            return
        }
        attemptPair(host.trim().ifEmpty { null }, trimmedKey, host2.trim().ifEmpty { null })
    }

    private fun attemptPair(host: String?, key: String, host2: String? = null) {
        _state.value = PairingState.Verifying
        viewModelScope.launch {
            val result = repository.pairAndVerify(PairingPayload(host, host2, key), host, host2)
            _state.value = result.fold(
                onSuccess = { PairingState.Success },
                onFailure = { e -> PairingState.Error(e.message ?: "Couldn't reach that server — check the host and that it's reachable from this phone.") },
            )
        }
    }

    fun isPaired(): Boolean = credentials.isPaired
}

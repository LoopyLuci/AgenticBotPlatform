package com.botserver.mobile.ui.pairing

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.CredentialStore
import com.botserver.mobile.data.PairingPayload
import com.botserver.mobile.data.PairingRepository
import com.botserver.mobile.diagnostics.AppLog
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

private const val TAG = "Pairing"

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
        attemptPair(payload.host, payload.key, payload.host2, payload.host3)
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
        attemptPair(payload.host, payload.key, payload.host2, payload.host3)
    }

    fun onManualSubmit(host: String, key: String, host2: String = "", host3: String = "") {
        AppLog.i(TAG, "onManualSubmit tapped: host=${host.ifBlank { "(blank)" }}, host2=${host2.ifBlank { "(blank)" }}, host3=${host3.ifBlank { "(blank)" }}, keyLen=${key.trim().length}")
        val trimmedKey = key.trim()
        if (trimmedKey.isEmpty()) {
            AppLog.w(TAG, "onManualSubmit: rejected — key field was empty")
            _state.value = PairingState.Error("Paste the key from the dashboard's Mobile tab.")
            return
        }
        attemptPair(host.trim().ifEmpty { null }, trimmedKey, host2.trim().ifEmpty { null }, host3.trim().ifEmpty { null })
    }

    private fun attemptPair(host: String?, key: String, host2: String? = null, host3: String? = null) {
        AppLog.i(TAG, "attemptPair: starting verification against host=$host host2=$host2 host3=$host3")
        _state.value = PairingState.Verifying
        viewModelScope.launch {
            try {
                val result = repository.pairAndVerify(PairingPayload(host, host2, host3, key), host, host2, host3)
                _state.value = result.fold(
                    onSuccess = {
                        AppLog.i(TAG, "attemptPair: succeeded")
                        PairingState.Success
                    },
                    onFailure = { e ->
                        AppLog.w(TAG, "attemptPair: failed — ${e::class.simpleName}: ${e.message}", e)
                        PairingState.Error(e.message ?: "Couldn't reach that server — check the host and that it's reachable from this phone.")
                    },
                )
            } catch (e: Exception) {
                // pairAndVerify() already wraps its own network call in
                // runCatching — this outer catch is specifically for
                // anything unexpected happening around it (e.g. a
                // CredentialStore/Keystore write failing), so a truly
                // unforeseen failure still updates state and gets logged
                // instead of silently killing this coroutine and leaving
                // the button stuck on "Verifying" forever with no visible
                // sign of what happened — see AppLog's own doc for why
                // that "looks like nothing happened" symptom matters.
                AppLog.e(TAG, "attemptPair: unexpected exception outside pairAndVerify's own error handling", e)
                _state.value = PairingState.Error(e.message ?: "Unexpected error — check exported diagnostics in Settings.")
            }
        }
    }

    fun isPaired(): Boolean = credentials.isPaired
}

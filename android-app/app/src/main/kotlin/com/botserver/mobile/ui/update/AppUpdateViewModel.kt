package com.botserver.mobile.ui.update

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.GitHubRelease
import com.botserver.mobile.data.GitHubUpdateRepository
import com.botserver.mobile.data.UpdateRepository
import com.botserver.mobile.diagnostics.AppLog
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import java.io.File
import javax.inject.Inject

private const val TAG = "GitHubUpdate"

sealed interface AppUpdateState {
    data object Idle : AppUpdateState
    data object Checking : AppUpdateState
    data object UpToDate : AppUpdateState
    data class Available(val release: GitHubRelease) : AppUpdateState
    data object Downloading : AppUpdateState
    data class Downloaded(val file: File) : AppUpdateState
    data class Error(val message: String) : AppUpdateState
}

/**
 * Self-update from the project's public GitHub releases — deliberately
 * independent of CredentialStore/pairing entirely, so both the
 * pre-pairing PairingScreen and the paired Settings screen can share one
 * instance of this logic (Hilt view-model scoping means each screen
 * still gets its own instance/state, but the *behavior* — check, decide
 * new-vs-seen, download, hand off to install — lives here exactly once).
 * See GitHubUpdateRepository's own doc for why there's no real semantic
 * version comparison, and why this works even when pairing is broken.
 */
@HiltViewModel
class AppUpdateViewModel @Inject constructor(
    private val gitHubUpdateRepository: GitHubUpdateRepository,
    private val updateRepository: UpdateRepository,
) : ViewModel() {

    private val _state = MutableStateFlow<AppUpdateState>(AppUpdateState.Idle)
    val state: StateFlow<AppUpdateState> = _state

    fun checkForUpdate() {
        _state.value = AppUpdateState.Checking
        viewModelScope.launch {
            _state.value = runCatching { gitHubUpdateRepository.checkLatest() }.fold(
                onSuccess = { release ->
                    when {
                        release == null -> AppUpdateState.Error("Latest release has no APK attached.")
                        gitHubUpdateRepository.isNew(release) -> AppUpdateState.Available(release)
                        else -> AppUpdateState.UpToDate
                    }
                },
                onFailure = { e ->
                    AppLog.w(TAG, "check failed", e)
                    AppUpdateState.Error(e.message ?: "Couldn't reach GitHub.")
                },
            )
        }
    }

    /** One tap covers download + handing off to the OS install prompt
     * (see the Downloaded state's consumer) — the only step that can't be
     * automated away is Android's own install-confirmation dialog, a
     * security boundary no ordinary app can skip. */
    fun downloadAndInstall() {
        val current = _state.value
        if (current !is AppUpdateState.Available) return
        _state.value = AppUpdateState.Downloading
        viewModelScope.launch {
            _state.value = runCatching { gitHubUpdateRepository.download(current.release) }.fold(
                onSuccess = {
                    gitHubUpdateRepository.markSeen(current.release)
                    AppUpdateState.Downloaded(it)
                },
                onFailure = { e ->
                    AppLog.w(TAG, "download failed", e)
                    AppUpdateState.Error(e.message ?: "Download failed.")
                },
            )
        }
    }

    fun dismiss() {
        (_state.value as? AppUpdateState.Available)?.let { gitHubUpdateRepository.markSeen(it.release) }
        _state.value = AppUpdateState.Idle
    }

    /** Same install flow the server-push update path (DevicesViewModel)
     * already uses — one FileProvider + ACTION_VIEW mechanism regardless
     * of whether the APK came from the paired server or GitHub. */
    fun installIntent(file: File) = updateRepository.installIntent(file)
}

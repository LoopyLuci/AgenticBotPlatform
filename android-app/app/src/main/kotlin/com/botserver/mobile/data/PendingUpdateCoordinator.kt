package com.botserver.mobile.data

import com.botserver.mobile.push.UpdatePushSignal
import java.io.File
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Process-wide (not screen-scoped, unlike a plain ViewModel) owner of
 * "is there a pending APK update, and how's the download going" —
 * DevicesViewModel used to own this directly, but that meant it only
 * existed, and could only react to a push, while the Devices screen
 * itself was on screen. Injected eagerly into [com.botserver.mobile.BotServerApp]
 * so it's alive (and collecting [UpdatePushSignal]) for the whole process
 * lifetime, the same way [com.botserver.mobile.diagnostics.AppLog] is.
 *
 * A push notification (see FcmService's "apk_update" handling) means an
 * operator *just* queued this specific device an update — that's a much
 * stronger, more deliberate signal than "the user happened to open the
 * Devices screen," so [checkForUpdate] auto-starts the download in that
 * case instead of waiting for an extra tap. A plain screen-open check
 * (no push involved) keeps the existing "Available -> user taps
 * Download" flow, since that path has no such explicit trigger backing
 * an unprompted data download.
 */
@Singleton
class PendingUpdateCoordinator @Inject constructor(
    private val updateRepository: UpdateRepository,
    private val updatePushSignal: UpdatePushSignal,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    private val _updateState = MutableStateFlow<UpdateState>(UpdateState.None)
    val updateState: StateFlow<UpdateState> = _updateState.asStateFlow()

    init {
        scope.launch {
            updatePushSignal.events.collect {
                checkForUpdate(autoDownload = true)
            }
        }
    }

    fun checkForUpdate(autoDownload: Boolean = false) {
        scope.launch {
            runCatching { updateRepository.checkPending() }.onSuccess { resp ->
                if (resp.available && resp.pushId != null) {
                    _updateState.value = UpdateState.Available(resp.pushId, resp.versionLabel, resp.mesh)
                    if (autoDownload) downloadUpdate()
                } else if (_updateState.value !is UpdateState.Downloading) {
                    _updateState.value = UpdateState.None
                }
            }
        }
    }

    fun downloadUpdate() {
        val current = _updateState.value
        if (current !is UpdateState.Available) return
        scope.launch {
            _updateState.value = UpdateState.Downloading()
            _updateState.value = runCatching {
                updateRepository.downloadApk(current.pushId, current.mesh) { progress ->
                    _updateState.value = UpdateState.Downloading(progress)
                }
            }.fold(
                onSuccess = { UpdateState.Downloaded(it) },
                onFailure = { e -> UpdateState.Error(e.message ?: "Download failed.") },
            )
        }
    }

    fun installIntent(file: File) = updateRepository.installIntent(file)

    fun dismissUpdate() {
        _updateState.value = UpdateState.None
    }
}

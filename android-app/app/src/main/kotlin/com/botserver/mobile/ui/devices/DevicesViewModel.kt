package com.botserver.mobile.ui.devices

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.DevicesRepository
import com.botserver.mobile.data.MeshServer
import com.botserver.mobile.data.NewDevicePairing
import com.botserver.mobile.data.UpdateRepository
import com.botserver.mobile.data.dto.DeviceInfo
import com.botserver.mobile.data.dto.MeshOrigin
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import java.io.File
import javax.inject.Inject

sealed interface GenerateState {
    data object Idle : GenerateState
    data object Generating : GenerateState
    data class Ready(val pairing: NewDevicePairing) : GenerateState
    data class Error(val message: String) : GenerateState
}

sealed interface UpdateState {
    data object None : UpdateState
    data class Available(val pushId: Int, val versionLabel: String?, val mesh: MeshOrigin?) : UpdateState
    data object Downloading : UpdateState
    data class Downloaded(val file: File) : UpdateState
    data class Error(val message: String) : UpdateState
}

sealed interface SendState {
    data object Idle : SendState
    data class Sending(val targetId: Int?) : SendState
    data class Sent(val message: String) : SendState
    data class Error(val message: String) : SendState
}

@HiltViewModel
class DevicesViewModel @Inject constructor(
    private val repository: DevicesRepository,
    private val updateRepository: UpdateRepository,
    private val meshServer: MeshServer,
) : ViewModel() {

    private val _state = MutableStateFlow<GenerateState>(GenerateState.Idle)
    val state: StateFlow<GenerateState> = _state

    private val _devices = MutableStateFlow<List<DeviceInfo>>(emptyList())
    val devices: StateFlow<List<DeviceInfo>> = _devices

    private var presenceStarted = false

    private val _refreshing = MutableStateFlow(false)
    val refreshing: StateFlow<Boolean> = _refreshing

    private val _updateState = MutableStateFlow<UpdateState>(UpdateState.None)
    val updateState: StateFlow<UpdateState> = _updateState

    private val _sendState = MutableStateFlow<SendState>(SendState.Idle)
    val sendState: StateFlow<SendState> = _sendState

    /** Push the server's latest built APK to one other device — mirrors the
     * desktop dashboard's per-row "Send APK" button. */
    fun sendUpdateTo(device: DeviceInfo) {
        _sendState.value = SendState.Sending(device.id)
        viewModelScope.launch {
            _sendState.value = runCatching { updateRepository.sendTo(device.id) }
                .fold(
                    onSuccess = { SendState.Sent("Sent to ${device.label}.") },
                    onFailure = { e -> SendState.Error(e.message ?: "Couldn't send to ${device.label}.") },
                )
        }
    }

    fun sendUpdateToAll() {
        _sendState.value = SendState.Sending(null)
        viewModelScope.launch {
            _sendState.value = runCatching { updateRepository.sendToAll() }
                .fold(
                    onSuccess = { count -> SendState.Sent("Queued for $count device(s).") },
                    onFailure = { e -> SendState.Error(e.message ?: "Couldn't send to all devices.") },
                )
        }
    }

    fun dismissSendState() {
        _sendState.value = SendState.Idle
    }

    /** Checked once per screen visit (see LaunchedEffect in DevicesScreen)
     * — cheap enough (one small GET) that there's no need for a background
     * schedule beyond "whenever this screen is opened." */
    fun checkForUpdate() {
        viewModelScope.launch {
            runCatching { updateRepository.checkPending() }.onSuccess { resp ->
                _updateState.value = if (resp.available && resp.pushId != null) {
                    UpdateState.Available(resp.pushId, resp.versionLabel, resp.mesh)
                } else {
                    UpdateState.None
                }
            }
        }
    }

    fun downloadUpdate() {
        val current = _updateState.value
        if (current !is UpdateState.Available) return
        viewModelScope.launch {
            _updateState.value = UpdateState.Downloading
            _updateState.value = runCatching { updateRepository.downloadApk(current.pushId, current.mesh) }
                .fold(
                    onSuccess = { UpdateState.Downloaded(it) },
                    onFailure = { e -> UpdateState.Error(e.message ?: "Download failed.") },
                )
        }
    }

    /** Runs only while the Devices screen is visible (see DevicesScreen's
     * DisposableEffect) — lets other paired devices on the same network
     * pull this device's own installed APK directly for the lifetime of
     * that visit. Stopping when the screen closes is a deliberate,
     * documented scope limit for this first version, not an oversight: a
     * true always-on listener would need a foreground Service, which is a
     * separate, larger change (persistent notification, battery exemption
     * prompts) left for a later pass if this proves useful enough to want
     * always-on. */
    fun startMesh() = meshServer.start()

    fun stopMesh() = meshServer.stop()

    fun dismissUpdate() {
        _updateState.value = UpdateState.None
    }

    fun installIntent(file: File) = updateRepository.installIntent(file)

    /** Manual "Update Devices" pull — the live WebSocket (startPresence
     * below) is the primary path, but it's a single long-lived connection
     * per device and isn't always reliable in practice (backgrounding,
     * flaky networks, a dropped socket that hasn't reconnected yet), so a
     * device can sit showing a stale list with no obvious sign anything's
     * wrong. This re-fetches the same DB-backed snapshot GET /api/devices
     * already serves for the initial load, on demand. */
    fun refreshDevices() {
        viewModelScope.launch {
            _refreshing.value = true
            runCatching { repository.devices() }.onSuccess { _devices.value = it }
            _refreshing.value = false
        }
    }

    /** Initial snapshot, then a live WebSocket for deltas — reconnecting
     * with a short backoff if it drops, so this device sees its siblings
     * go online/offline in real time (that's the actual point: pair a new
     * device from a phone and watch it show up on the tablet). */
    fun startPresence() {
        if (presenceStarted) return
        presenceStarted = true
        viewModelScope.launch {
            runCatching { repository.devices() }.onSuccess { _devices.value = it }
            while (true) {
                runCatching {
                    repository.liveDevices().collect { list -> _devices.value = list }
                }
                delay(4000)
            }
        }
    }

    fun generate(label: String) {
        _state.value = GenerateState.Generating
        viewModelScope.launch {
            _state.value = runCatching { repository.createPairingForNewDevice(label.ifBlank { "New device" }) }
                .fold(
                    onSuccess = { GenerateState.Ready(it) },
                    onFailure = { e -> GenerateState.Error(e.message ?: "Couldn't generate a key — check your connection.") },
                )
            // A newly-minted key doesn't show up in the presence list until
            // that device actually connects, but re-fetching the snapshot
            // keeps this screen from feeling stale meanwhile.
            runCatching { repository.devices() }.onSuccess { _devices.value = it }
        }
    }

    fun reset() {
        _state.value = GenerateState.Idle
    }
}

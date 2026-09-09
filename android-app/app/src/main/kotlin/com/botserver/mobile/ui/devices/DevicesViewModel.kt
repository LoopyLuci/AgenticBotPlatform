package com.botserver.mobile.ui.devices

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.DeviceTiers
import com.botserver.mobile.data.DevicesRepository
import com.botserver.mobile.data.MeshServer
import com.botserver.mobile.data.NewDevicePairing
import com.botserver.mobile.data.PendingUpdateCoordinator
import com.botserver.mobile.data.ServerChatRepository
import com.botserver.mobile.data.UpdateRepository
import com.botserver.mobile.data.UpdateState
import com.botserver.mobile.data.WebRtcMeshClient
import com.botserver.mobile.data.dto.DeviceInfo
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch
import java.io.File
import javax.inject.Inject

sealed interface GenerateState {
    data object Idle : GenerateState
    data object Generating : GenerateState
    data class Ready(val pairing: NewDevicePairing) : GenerateState
    data class Error(val message: String) : GenerateState
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
    private val pendingUpdateCoordinator: PendingUpdateCoordinator,
    private val serverChatRepository: ServerChatRepository,
    private val meshServer: MeshServer,
    private val webRtcMeshClient: WebRtcMeshClient,
) : ViewModel() {

    private val _state = MutableStateFlow<GenerateState>(GenerateState.Idle)
    val state: StateFlow<GenerateState> = _state

    // One-shot feedback for "Message this device" — see the chat
    // ViewModels' identical snackbarMessages field for why this exists
    // rather than a plain error StateFlow.
    private val _snackbarMessages = kotlinx.coroutines.flow.MutableSharedFlow<String>(extraBufferCapacity = 1)
    val snackbarMessages: kotlinx.coroutines.flow.SharedFlow<String> = _snackbarMessages

    /** Opens (or re-opens, if it was previously fully deleted from
     * Server Chat) a direct conversation with [device], then navigates
     * to it — via [onOpened], called with [device].id — the way back to
     * a device after "Delete chat" removed the conversation entirely.
     * The Server Chat screen's own ViewModel is a separate instance
     * (scoped to its own nav back-stack entry), so it has no way to know
     * a specific conversation should open just because this screen's
     * pre-flight call succeeded — [onOpened]'s argument is what actually
     * tells it which one, threaded through as a nav argument. */
    fun messageDevice(device: DeviceInfo, onOpened: (Int) -> Unit) {
        viewModelScope.launch {
            runCatching { serverChatRepository.openConversation(device.id) }
                .onSuccess { onOpened(device.id) }
                .onFailure { _snackbarMessages.tryEmit(it.message ?: "Couldn't message ${device.label}.") }
        }
    }

    private val _devices = MutableStateFlow<List<DeviceInfo>>(emptyList())
    val devices: StateFlow<List<DeviceInfo>> = _devices

    // This device's own identity/tier, resolved once from Server Chat's
    // whoami (0 is a safe "not yet resolved" sentinel — it can never
    // match a real /api/devices row, since desktop, the only caller that
    // whoami maps to 0, has no api_keys row of its own at all). myTier
    // defaults to "none" (hide everything) rather than assuming
    // "unrestricted" until the real row is actually found — a wrong
    // client-side guess here can only ever hide a manage action the
    // server would have allowed, never show one it would refuse.
    private val _myDeviceId = MutableStateFlow(0)
    val myDeviceId: StateFlow<Int> = _myDeviceId

    private val _myTier = MutableStateFlow("none")
    val myTier: StateFlow<String> = _myTier

    init {
        viewModelScope.launch {
            runCatching { serverChatRepository.myDeviceId() }.onSuccess { _myDeviceId.value = it }
        }
        viewModelScope.launch {
            combine(_devices, _myDeviceId) { devices, myId -> devices.find { it.id == myId }?.permissionTier ?: "none" }
                .collect { _myTier.value = it }
        }
    }

    private var presenceStarted = false

    private val _refreshing = MutableStateFlow(false)
    val refreshing: StateFlow<Boolean> = _refreshing

    // Delegates to the process-wide coordinator (see its own doc) rather
    // than owning this state — a push-triggered download can start and
    // finish while the Devices screen isn't even open.
    val updateState: StateFlow<UpdateState> = pendingUpdateCoordinator.updateState

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
     * schedule beyond "whenever this screen is opened," on top of the
     * coordinator's own push-triggered checks. */
    fun checkForUpdate() = pendingUpdateCoordinator.checkForUpdate()

    fun downloadUpdate() = pendingUpdateCoordinator.downloadUpdate()

    /** Runs only while the Devices screen is visible (see DevicesScreen's
     * DisposableEffect) — lets other paired devices on the same network
     * pull this device's own installed APK directly for the lifetime of
     * that visit. Stopping when the screen closes is a deliberate,
     * documented scope limit for this first version, not an oversight: a
     * true always-on listener would need a foreground Service, which is a
     * separate, larger change (persistent notification, battery exemption
     * prompts) left for a later pass if this proves useful enough to want
     * always-on. */
    fun startMesh() {
        meshServer.start()
        webRtcMeshClient.start()
    }

    fun stopMesh() {
        meshServer.stop()
        webRtcMeshClient.stop()
    }

    fun dismissUpdate() = pendingUpdateCoordinator.dismissUpdate()

    fun installIntent(file: File) = pendingUpdateCoordinator.installIntent(file)

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

    fun generate(label: String, tier: String = "none") {
        _state.value = GenerateState.Generating
        viewModelScope.launch {
            _state.value = runCatching { repository.createPairingForNewDevice(label.ifBlank { "New device" }, tier) }
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

    /** Tiers this device may currently offer minting a new device at,
     * capped at its own tier — see DeviceTiers.mintableTiers(). */
    fun mintableTiers(): List<String> = DeviceTiers.mintableTiers(_myTier.value)

    /** Whether this device's own tier permits changing [target]'s tier or
     * revoking it — decides only whether the UI *offers* the action; the
     * server enforces the real check on every call regardless. */
    fun canManage(target: DeviceInfo): Boolean =
        DeviceTiers.canManage(_myTier.value, target.permissionTier, isSelf = target.id == _myDeviceId.value)

    fun changeDeviceTier(device: DeviceInfo, newTier: String) {
        viewModelScope.launch {
            runCatching { repository.setDeviceTier(device.id, newTier) }
                .onSuccess {
                    _snackbarMessages.tryEmit("${device.label} is now '$newTier'.")
                    refreshDevices()
                }
                .onFailure { e -> _snackbarMessages.tryEmit(e.message ?: "Couldn't change ${device.label}'s tier.") }
        }
    }

    fun revokeDevice(device: DeviceInfo) {
        viewModelScope.launch {
            runCatching { repository.revokeDevice(device.id) }
                .onSuccess {
                    _snackbarMessages.tryEmit("${device.label} revoked.")
                    refreshDevices()
                }
                .onFailure { e -> _snackbarMessages.tryEmit(e.message ?: "Couldn't revoke ${device.label}.") }
        }
    }
}

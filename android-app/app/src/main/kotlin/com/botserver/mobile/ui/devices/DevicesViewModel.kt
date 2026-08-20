package com.botserver.mobile.ui.devices

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.DevicesRepository
import com.botserver.mobile.data.NewDevicePairing
import com.botserver.mobile.data.dto.DeviceInfo
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

sealed interface GenerateState {
    data object Idle : GenerateState
    data object Generating : GenerateState
    data class Ready(val pairing: NewDevicePairing) : GenerateState
    data class Error(val message: String) : GenerateState
}

@HiltViewModel
class DevicesViewModel @Inject constructor(private val repository: DevicesRepository) : ViewModel() {

    private val _state = MutableStateFlow<GenerateState>(GenerateState.Idle)
    val state: StateFlow<GenerateState> = _state

    private val _devices = MutableStateFlow<List<DeviceInfo>>(emptyList())
    val devices: StateFlow<List<DeviceInfo>> = _devices

    private var presenceStarted = false

    private val _refreshing = MutableStateFlow(false)
    val refreshing: StateFlow<Boolean> = _refreshing

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

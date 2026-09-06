package com.botserver.mobile.data

import com.botserver.mobile.data.dto.MeshOrigin
import java.io.File

/** Lives in `data`, not `ui/devices`, because [PendingUpdateCoordinator]
 * (a process-wide singleton, not scoped to the Devices screen) owns this
 * state now — a push-triggered update needs to be visible/actionable
 * regardless of which screen happens to be open when it arrives. */
sealed interface UpdateState {
    data object None : UpdateState
    data class Available(val pushId: Int, val versionLabel: String?, val mesh: MeshOrigin?) : UpdateState

    /** [progress] is 0f..1f when the server reported a Content-Length
     * (the plain server-relay download path), or -1f for "unknown" — the
     * mesh/WebRTC transfer paths don't currently report byte-level
     * progress, so those stay indeterminate. */
    data class Downloading(val progress: Float = -1f) : UpdateState
    data class Downloaded(val file: File) : UpdateState
    data class Error(val message: String) : UpdateState
}

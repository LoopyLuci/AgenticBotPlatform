package com.botserver.mobile.push

import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow

/** The wake-up wire between [FcmService] (which has no coroutine scope of
 * its own worth keeping alive, and shouldn't reach into a ViewModel
 * directly) and [com.botserver.mobile.data.PendingUpdateCoordinator]
 * (which does the actual "check pending, start downloading" work).
 * `replay = 1` so a push that arrives before anything is collecting yet
 * (e.g. the coordinator singleton hasn't been created because the app
 * process just cold-started from the notification tap) still gets
 * picked up the moment the coordinator's collector starts. */
@Singleton
class UpdatePushSignal @Inject constructor() {
    private val _events = MutableSharedFlow<Unit>(replay = 1, extraBufferCapacity = 1)
    val events: SharedFlow<Unit> = _events.asSharedFlow()

    fun emit() {
        _events.tryEmit(Unit)
    }
}

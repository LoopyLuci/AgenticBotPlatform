package com.botserver.mobile.data

import com.botserver.mobile.data.dto.RegisterPushTokenRequest
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class PushRepository @Inject constructor(
    private val apiService: ApiService,
    private val credentials: CredentialStore,
) {
    /** Registers this device's current FCM token with the paired server —
     * a no-op if not yet paired (FcmService.onNewToken can fire before
     * pairing completes; the token is re-sent once pairing finishes since
     * FCM tokens are stable across that window). */
    suspend fun registerToken(token: String) {
        if (!credentials.isPaired) return
        runCatching { apiService.registerPushToken(RegisterPushTokenRequest(token)) }
    }
}

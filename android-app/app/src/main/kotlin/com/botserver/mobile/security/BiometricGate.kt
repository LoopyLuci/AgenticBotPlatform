package com.botserver.mobile.security

import android.content.Context
import android.content.ContextWrapper
import android.util.Log
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_WEAK
import androidx.biometric.BiometricManager.Authenticators.DEVICE_CREDENTIAL
import androidx.biometric.BiometricPrompt
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine

private const val TAG = "BiometricGate"
private const val ALLOWED_AUTHENTICATORS = BIOMETRIC_WEAK or DEVICE_CREDENTIAL

/**
 * Gates a sensitive action (delete a bot, approve a pairing, view/edit a
 * token, push an update APK to another device) behind the device's own
 * lock-screen check — fingerprint/face if enrolled, PIN/pattern/password
 * otherwise. Anyone who can unlock the phone already has full access to
 * this data via the OS, so this isn't a second independent secret; it's a
 * deliberate extra tap before an irreversible or sensitive action, same
 * rationale apps like password managers use for "confirm it's still you."
 *
 * Devices with no secure lock screen at all (no PIN/pattern/password, no
 * biometric — `BIOMETRIC_STATUS_UNKNOWN`/`BIOMETRIC_ERROR_NONE_ENROLLED`
 * with no device credential either) can't satisfy this, so the gate is
 * skipped rather than permanently locking those users out of their only
 * path to the action.
 */
suspend fun requireBiometricAuth(activity: FragmentActivity, title: String): Boolean {
    val manager = BiometricManager.from(activity)
    if (manager.canAuthenticate(ALLOWED_AUTHENTICATORS) != BiometricManager.BIOMETRIC_SUCCESS) {
        Log.w(TAG, "no usable biometric/device-credential enrolled — skipping gate for \"$title\"")
        return true
    }
    val promptInfo = BiometricPrompt.PromptInfo.Builder()
        .setTitle(title)
        .setAllowedAuthenticators(ALLOWED_AUTHENTICATORS)
        .build()
    return suspendCancellableCoroutine { cont ->
        val prompt = BiometricPrompt(
            activity,
            ContextCompat.getMainExecutor(activity),
            object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    if (cont.isActive) cont.resume(true)
                }
                override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                    if (cont.isActive) cont.resume(false)
                }
                override fun onAuthenticationFailed() {
                    // A single failed attempt (wrong fingerprint) — the
                    // prompt stays open for a retry, don't resolve yet.
                }
            },
        )
        cont.invokeOnCancellation { prompt.cancelAuthentication() }
        prompt.authenticate(promptInfo)
    }
}

/** Walks up ContextWrapper layers to find the hosting FragmentActivity —
 * needed because a Composable's LocalContext is usually the Activity
 * itself but can be wrapped (e.g. by a ContextThemeWrapper). */
fun Context.findFragmentActivity(): FragmentActivity? {
    var ctx = this
    while (ctx is ContextWrapper) {
        if (ctx is FragmentActivity) return ctx
        ctx = ctx.baseContext
    }
    return null
}

@Composable
fun rememberFragmentActivity(): FragmentActivity? {
    val context = LocalContext.current
    return remember(context) { context.findFragmentActivity() }
}

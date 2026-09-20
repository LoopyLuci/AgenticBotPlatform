package com.agenticbotplatform.mobile.data

import java.io.IOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import retrofit2.HttpException

/**
 * What to tell a person when a request failed — instead of the raw exception
 * text ("failed to connect to /192.168.69.145 (port 8791) from /192.168.69.248
 * (port 39928) after 6000ms"), which is the address of whatever was tried and
 * says nothing about what to do. Screens pair this with a Retry button
 * (ui/components/StateViews.kt's ErrorState).
 */
object UserFacingError {
    const val UNREACHABLE =
        "Can't reach your server. Make sure it's running and that this device can reach it " +
            "(same Wi-Fi, or Tailscale connected), then try again."
    const val UNAUTHORIZED =
        "Your server didn't accept this device's key. It may have been revoked — pair this device again from Settings."

    fun message(error: Throwable?, fallback: String = "Something went wrong. Please try again."): String {
        var e: Throwable? = error
        // Walk the cause chain: a transport failure is often wrapped.
        while (e != null) {
            when (e) {
                is HttpException -> return when (e.code()) {
                    401, 403 -> UNAUTHORIZED
                    in 500..599 -> "Your server had a problem handling that (HTTP ${e.code()}). Try again in a moment."
                    else -> "The server answered with an error (HTTP ${e.code()})."
                }
                is UnknownHostException, is ConnectException, is SocketTimeoutException -> return UNREACHABLE
                is IOException -> return UNREACHABLE
            }
            e = e.cause?.takeIf { it !== e }
        }
        return error?.message?.takeIf { it.isNotBlank() } ?: fallback
    }
}

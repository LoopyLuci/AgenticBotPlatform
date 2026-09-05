package com.botserver.mobile

import android.app.Application
import coil.Coil
import coil.ImageLoader
import com.botserver.mobile.diagnostics.AppLog
import dagger.hilt.android.HiltAndroidApp
import javax.inject.Inject

@HiltAndroidApp
class BotServerApp : Application() {

    // Installed as Coil's process-wide default so every AsyncImage in the
    // app (chat attachment thumbnails) goes through the same authenticated,
    // host-failover-aware OkHttpClient as Retrofit — see
    // di/NetworkModule.kt's provideImageLoader().
    @Inject lateinit var imageLoader: ImageLoader

    override fun onCreate() {
        super.onCreate()
        AppLog.init(this)
        // Chains onto (never replaces) whatever handler was already
        // installed — Android's own default handler shows the "app has
        // stopped" dialog and terminates the process; this only adds a
        // durable record of *why* before that happens, since a crash the
        // instant a button is tapped otherwise leaves nothing behind once
        // the process dies (Logcat resets on relaunch, ADB may not even
        // be available on a phone that's away from this machine).
        val previousHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            AppLog.e("Crash", "Uncaught exception on thread ${thread.name}", throwable)
            previousHandler?.uncaughtException(thread, throwable)
        }
        Coil.setImageLoader(imageLoader)
    }
}

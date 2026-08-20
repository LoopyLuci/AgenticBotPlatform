package com.botserver.mobile

import android.app.Application
import coil.Coil
import coil.ImageLoader
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
        Coil.setImageLoader(imageLoader)
    }
}

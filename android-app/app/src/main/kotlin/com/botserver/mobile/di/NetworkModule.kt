package com.botserver.mobile.di

import android.content.Context
import android.os.Build
import coil.ImageLoader
import com.botserver.mobile.BuildConfig
import com.botserver.mobile.data.ApiService
import com.botserver.mobile.data.CredentialStore
import com.botserver.mobile.data.MeshPortHolder
import com.botserver.mobile.data.PrivateNetworkGuard
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory
import java.io.IOException
import java.util.concurrent.TimeUnit
import javax.inject.Singleton

// Sent on every request so the server's device_presence can show what kind
// of device is connected, not just that one is — see bot/db.py's
// verify_api_key() and the X-Device-Platform/X-Device-App-Version headers
// it reads.
private const val DEVICE_PLATFORM = "android"

// Real hardware identity — distinct from the user-typed pairing label, so
// the dashboard's Devices view can show "Pixel 8 Pro (Android 14)" instead
// of whatever generic label a device happened to be paired under (e.g. a
// leftover "Browser Test Phone" from manual testing). MANUFACTURER is
// folded in only when MODEL doesn't already start with it (most OEMs
// already prefix their model strings — Samsung's don't) to avoid
// "Samsung Samsung SM-S911U"-style duplication.
private val DEVICE_MODEL: String = Build.MODEL.let { model ->
    if (model.startsWith(Build.MANUFACTURER, ignoreCase = true)) model
    else "${Build.MANUFACTURER} $model"
}
private val DEVICE_OS_VERSION: String = "Android ${Build.VERSION.RELEASE}"

// Every request (Retrofit, Coil, and any manually-built OkHttp Request
// like the thumbnail loads in ChatScreen) is issued against this fixed
// placeholder and rewritten to the real paired host by
// DynamicHostInterceptor below — see its class doc for why.
const val PLACEHOLDER_BASE_URL = "http://127.0.0.1:8787"

/**
 * Retrofit needs a base URL fixed at construction, but this app's base URL
 * (the paired server's host:port) is only known after pairing and can
 * change if the user re-pairs to a different server — so instead of a
 * fixed base URL, every request is issued against a placeholder and this
 * interceptor rewrites it to CredentialStore's current host right before
 * it goes out. This is the mobile equivalent of the web frontends' api()
 * helper always reading getToken()/API_BASE fresh on every call rather
 * than capturing it once.
 *
 * It's also where connection failover lives: a paired device can carry two
 * independent hosts (e.g. a LAN IP and a Tailscale hostname). Every request
 * goes to whichever one last worked; if that throws an IOException (host
 * unreachable, timed out, connection reset — not an HTTP error status,
 * which reaches the app normally), it's retried once against the other
 * host before giving up, and the winner is remembered so later requests
 * don't keep re-probing a dead path.
 */
private class DynamicHostInterceptor(private val credentials: CredentialStore) : Interceptor {
    private fun rebuild(original: Request, base: String): Request? {
        val target = base.toHttpUrlOrNull() ?: return null
        if (target.scheme == "http" && !PrivateNetworkGuard.isAllowedHost(target.host)) {
            // Refuse to send the auth token in cleartext to anything outside
            // Tailscale/private-LAN ranges — see PrivateNetworkGuard's doc.
            // Thrown as IOException so intercept()'s existing fallback path
            // treats this exactly like an unreachable host and tries the
            // other paired host instead of silently leaking the token.
            throw IOException("refusing cleartext request to non-private host: ${target.host}")
        }
        val newUrl = original.url.newBuilder()
            .scheme(target.scheme)
            .host(target.host)
            .port(target.port)
            .build()
        val builder = original.newBuilder().url(newUrl)
        credentials.apiKey?.let { builder.header("X-Dashboard-Token", it) }
        builder.header("X-Device-Platform", DEVICE_PLATFORM)
        builder.header("X-Device-App-Version", BuildConfig.VERSION_NAME)
        builder.header("X-Device-Model", DEVICE_MODEL)
        builder.header("X-Device-OS-Version", DEVICE_OS_VERSION)
        // Self-reported, live: whatever port MeshServer is bound to right
        // now (0/absent when it isn't running), so the server always knows
        // exactly where another device could reach this one directly.
        MeshPortHolder.port.takeIf { it > 0 }?.let { builder.header("X-Mesh-Port", it.toString()) }
        return builder.build()
    }

    override fun intercept(chain: Interceptor.Chain): Response {
        val original = chain.request()
        val primaryBase = credentials.preferredBaseUrl()
        try {
            val primaryReq = rebuild(original, primaryBase) ?: original
            val response = chain.proceed(primaryReq)
            credentials.markGood(primaryBase)
            return response
        } catch (e: IOException) {
            val fallbackBase = credentials.otherBaseUrl() ?: throw e
            val fallbackReq = rebuild(original, fallbackBase) ?: throw e
            val response = chain.proceed(fallbackReq)
            credentials.markGood(fallbackBase)
            return response
        }
    }
}

@Module
@InstallIn(SingletonComponent::class)
object NetworkModule {

    @Provides
    @Singleton
    fun provideOkHttpClient(credentials: CredentialStore): OkHttpClient =
        OkHttpClient.Builder()
            // Short-ish connect timeout so a dead host fails fast into the
            // interceptor's fallback instead of stalling the UI for
            // OkHttp's 10s default before the other path even gets tried.
            .connectTimeout(6, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .writeTimeout(15, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .addInterceptor(DynamicHostInterceptor(credentials))
            .build()

    @Provides
    @Singleton
    fun provideJson(): Json = Json { ignoreUnknownKeys = true; isLenient = true }

    @Provides
    @Singleton
    fun provideRetrofit(client: OkHttpClient, json: Json): Retrofit =
        Retrofit.Builder()
            // Placeholder — every real request's host is overwritten by
            // DynamicHostInterceptor above before it leaves the device.
            .baseUrl(PLACEHOLDER_BASE_URL)
            .client(client)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()

    @Provides
    @Singleton
    fun provideApiService(retrofit: Retrofit): ApiService = retrofit.create(ApiService::class.java)

    // Reuses the same OkHttpClient (auth header + host failover) so a
    // thumbnail request behaves exactly like any Retrofit call — one auth
    // mechanism for the whole app, not a second one just for images.
    @Provides
    @Singleton
    fun provideImageLoader(@ApplicationContext context: Context, client: OkHttpClient): ImageLoader =
        ImageLoader.Builder(context).okHttpClient(client).build()
}

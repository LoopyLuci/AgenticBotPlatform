plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.hilt.android)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.google.services)
    alias(libs.plugins.ksp)
}

android {
    namespace = "com.botserver.mobile"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.botserver.mobile"
        minSdk = 26
        targetSdk = 35
        versionCode = 3
        versionName = "1.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        create("release") {
            // The same debug keystore Android Studio/the SDK generates by
            // default (~/.android/debug.keystore, standard "androiddebugkey"
            // / "android" credentials) — not a Play Store signing identity.
            // This app is sideloaded, not published through Play, so a
            // debug-keyed release build is the actual distribution model,
            // not a placeholder; using it here makes `assembleRelease`
            // reproducibly produce a signed, installable APK on its own
            // instead of needing a manual signing step after every build.
            storeFile = file(System.getProperty("user.home") + "/.android/debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }
    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.14"
    }

    packaging {
        // MockK's Android artifact pulls in byte-buddy-agent, which bundles
        // native attach helpers under these paths — irrelevant on Android
        // (nothing here uses JVM-agent attach), but without excluding them
        // the androidTest APK fails to package with a duplicate-file error.
        resources {
            excludes += "META-INF/LICENSE*"
            excludes += "win32-x86/attach_hotspot_windows.dll"
            excludes += "win32-x86-64/attach_hotspot_windows.dll"
        }
    }

    testOptions {
        unitTests {
            // Plain JVM unit tests (no emulator/Robolectric) — a few
            // classes under test touch Android framework fields
            // incidentally (e.g. android.os.Build.MODEL for a header
            // value) without the test caring what they return. Without
            // this, any unstubbed Android SDK call throws instead of
            // returning a default, even where the test's actual
            // assertions never depend on it.
            isReturnDefaultValues = true
        }
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.activity.compose)

    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.navigation.compose)

    // Networking — mirrors the dashboard/desktop frontends' api() helper:
    // one client, one auth header interceptor sourcing the stored key.
    implementation(libs.retrofit.core)
    implementation(libs.retrofit.converter.kotlinx.serialization)
    implementation(libs.okhttp.core)
    implementation(libs.okhttp.logging.interceptor)
    implementation(libs.kotlinx.serialization.json)

    // DI
    implementation(libs.hilt.android)
    ksp(libs.hilt.android.compiler)
    implementation(libs.androidx.hilt.navigation.compose)

    // Offline cache — bot list is the source of truth for the UI, refreshed
    // from the network rather than fetched fresh on every screen visit. See
    // data/db/AppDatabase.kt for why credentials are deliberately excluded
    // from what gets persisted here.
    implementation(libs.androidx.room.runtime)
    implementation(libs.androidx.room.ktx)
    ksp(libs.androidx.room.compiler)

    // Encrypted, Keystore-backed credential storage — the mobile analog of
    // the web frontends' localStorage token, but with real encryption at
    // rest, appropriate for a device that leaves the house.
    implementation(libs.androidx.security.crypto)

    // Biometric/device-credential gate for sensitive actions (delete bot,
    // approve pairing, view/edit tokens, mesh APK push) — see
    // security/BiometricGate.kt.
    implementation(libs.androidx.biometric)

    // Push notifications (Phase B-5)
    implementation(platform(libs.firebase.bom))
    implementation(libs.firebase.messaging.ktx)
    implementation(libs.kotlinx.coroutines.play.services)

    // QR pairing scan
    implementation(libs.androidx.camera.core)
    implementation(libs.androidx.camera.camera2)
    implementation(libs.androidx.camera.lifecycle)
    implementation(libs.androidx.camera.view)
    implementation(libs.mlkit.barcode.scanning)
    implementation(libs.androidx.compose.material.icons.extended)

    // Attachment thumbnails in the chat list — decode/cache/scale, backed
    // by the same OkHttpClient (and its auth/host-failover interceptor) the
    // rest of the app already uses, see di/NetworkModule.kt's ImageLoader.
    implementation(libs.coil.compose)

    // WebRTC data channels — the cross-network half of the mesh APK
    // transfer (see data/WebRtcMeshClient.kt): used purely for a
    // PeerConnection + DataChannel, no audio/video capture, as the fallback
    // when two devices aren't on the same LAN for MeshServer.kt's direct
    // socket path. Google stopped publishing org.webrtc:google-webrtc to
    // Maven Central; this is the actively maintained community build.
    implementation(libs.stream.webrtc.android)

    testImplementation(libs.junit)
    testImplementation(libs.mockk)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver)
    testImplementation(libs.turbine)
    androidTestImplementation(libs.androidx.test.ext.junit)
    androidTestImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.androidx.test.rules)
    androidTestImplementation(libs.androidx.test.espresso.core)
    // The compose-bom platform only applies within the configuration it's
    // declared in — androidTest needs its own, matching the versions the
    // main `implementation(platform(...))` above already pins.
    androidTestImplementation(platform(libs.androidx.compose.bom))
    androidTestImplementation(libs.androidx.compose.ui.test.junit4)
    androidTestImplementation(libs.mockk.android)
    androidTestImplementation(libs.kotlinx.coroutines.test)
    debugImplementation(libs.androidx.compose.ui.tooling)
    debugImplementation(libs.androidx.compose.ui.test.manifest)
}

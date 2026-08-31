plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.google.dagger.hilt.android")
    id("org.jetbrains.kotlin.plugin.serialization")
    id("com.google.gms.google-services")
    kotlin("kapt")
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
            isMinifyEnabled = false
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
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.4")
    implementation("androidx.activity:activity-compose:1.9.1")

    val composeBom = platform("androidx.compose:compose-bom:2024.06.00")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.navigation:navigation-compose:2.7.7")

    // Networking — mirrors the dashboard/desktop frontends' api() helper:
    // one client, one auth header interceptor sourcing the stored key.
    implementation("com.squareup.retrofit2:retrofit:2.11.0")
    implementation("com.squareup.retrofit2:converter-kotlinx-serialization:2.11.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")

    // DI
    implementation("com.google.dagger:hilt-android:2.51.1")
    kapt("com.google.dagger:hilt-android-compiler:2.51.1")
    implementation("androidx.hilt:hilt-navigation-compose:1.2.0")

    // Encrypted, Keystore-backed credential storage — the mobile analog of
    // the web frontends' localStorage token, but with real encryption at
    // rest, appropriate for a device that leaves the house.
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // Push notifications (Phase B-5)
    implementation(platform("com.google.firebase:firebase-bom:33.1.2"))
    implementation("com.google.firebase:firebase-messaging-ktx")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-play-services:1.8.1")

    // QR pairing scan
    implementation("androidx.camera:camera-core:1.3.4")
    implementation("androidx.camera:camera-camera2:1.3.4")
    implementation("androidx.camera:camera-lifecycle:1.3.4")
    implementation("androidx.camera:camera-view:1.3.4")
    implementation("com.google.mlkit:barcode-scanning:17.3.0")
    implementation("androidx.compose.material:material-icons-extended:1.6.8")

    // Attachment thumbnails in the chat list — decode/cache/scale, backed
    // by the same OkHttpClient (and its auth/host-failover interceptor) the
    // rest of the app already uses, see di/NetworkModule.kt's ImageLoader.
    implementation("io.coil-kt:coil-compose:2.6.0")

    // WebRTC data channels — the cross-network half of the mesh APK
    // transfer (see data/WebRtcMeshClient.kt): used purely for a
    // PeerConnection + DataChannel, no audio/video capture, as the fallback
    // when two devices aren't on the same LAN for MeshServer.kt's direct
    // socket path. Google stopped publishing org.webrtc:google-webrtc to
    // Maven Central; this is the actively maintained community build.
    implementation("io.getstream:stream-webrtc-android:1.3.10")

    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    debugImplementation("androidx.compose.ui:ui-tooling")
}

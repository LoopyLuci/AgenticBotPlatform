// Top-level build file — plugin versions declared here, applied per-module.
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
    id("com.google.dagger.hilt.android") version "2.51.1" apply false
    id("org.jetbrains.kotlin.plugin.serialization") version "1.9.24" apply false
    // Push notifications (Phase B-5) — parses app/google-services.json at
    // build time. The checked-in file is a structurally-valid PLACEHOLDER;
    // see docs/mobile-access.md — replace it with your own Firebase
    // project's real download before push notifications will actually work.
    id("com.google.gms.google-services") version "4.4.2" apply false
}

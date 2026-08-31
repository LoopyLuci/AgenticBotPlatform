// Top-level build file — plugin versions declared here (via the version
// catalog, gradle/libs.versions.toml), applied per-module.
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.hilt.android) apply false
    alias(libs.plugins.kotlin.serialization) apply false
    alias(libs.plugins.ksp) apply false
    // Push notifications (Phase B-5) — parses app/google-services.json at
    // build time. The checked-in file is a structurally-valid PLACEHOLDER;
    // see docs/mobile-access.md — replace it with your own Firebase
    // project's real download before push notifications will actually work.
    alias(libs.plugins.google.services) apply false
}

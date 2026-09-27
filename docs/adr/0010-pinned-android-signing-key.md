# ADR-0010: The Android signing key is pinned into ABP's own keystore

**Status:** Accepted — supersedes the "no key to lose" consequence of [ADR-0004](0004-debug-signed-android-release.md)
**Date:** 2026-09-27

## Context

ADR-0004 signs release APKs with the SDK's debug keystore and says this
works "on any machine" with "no private key to generate, protect, or
lose". That is wrong. `~/.android/debug.keystore` holds a key pair that
is generated at random on each machine the first time the SDK needs it,
and regenerated silently if the file is deleted. The well-known part is
only the *password* (`android`), not the key.

So a build on a second PC, or on the same PC after a reinstall, is
signed with a different key. Android refuses to install an update whose
signature differs from the installed app, so every paired phone would
reject the next in-app update and need a manual uninstall, losing its
pairing and local data.

## Decision

On first build, `android-app/app/build.gradle.kts` copies the existing
debug keystore byte for byte to `~/.abp/android-release.keystore` (or
`ABP_ANDROID_KEYSTORE`), and from then on signs with that pinned copy.
The copy has the same key material, so installs that already exist keep
accepting updates. ABP's backup system (`bot/sentinel/backup.py`) keeps
copies of the pinned keystore with every backup set, so losing the
machine no longer means losing the signing identity.

The sideload distribution model and the debug-style credentials from
ADR-0004 are unchanged.

## Consequences

To build on a new machine, restore the keystore from a backup (or point
`ABP_ANDROID_KEYSTORE` at it) *before* the first build. Otherwise the SDK
generates a fresh debug key, the build pins that one, and existing phones
reject its updates, which is exactly the failure this ADR exists to prevent.
The provenance caveat in ADR-0004 still applies: this key proves
continuity, not identity.

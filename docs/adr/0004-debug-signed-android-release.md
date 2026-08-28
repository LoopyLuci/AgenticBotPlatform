# ADR-0004: Android release builds are signed with the debug key, not a Play identity

**Status:** Accepted
**Date:** 2026-08-23 (present since the Android companion's first release)

## Context

Android requires every APK to be signed. A "release" build type normally
implies a real, private signing identity used to publish to the Play
Store and to let future updates verify they come from the same
publisher. BotServer's Android app is not distributed through the Play
Store — it's downloaded from GitHub Releases and sideloaded.

## Decision

`android-app/app/build.gradle.kts`'s `release` build type signs with the
same debug keystore Android Studio/the SDK generates by default
(`~/.android/debug.keystore`, standard `androiddebugkey`/`android`
credentials) rather than a dedicated release identity.

## Consequences

`./gradlew assembleRelease` reproducibly produces a signed, installable
APK on its own, on any machine with the standard Android SDK tooling —
no manual signing step, no private key to generate, protect, or lose.
This is a deliberate reflection of the actual distribution model
(sideload), not a placeholder standing in for "real" signing to be
swapped in later.

The tradeoff: anyone can rebuild an APK signed with the same well-known
debug credentials, so this signature proves nothing about provenance —
it only lets Android accept the APK as internally consistent across
updates from the same build. If this project ever distributes through
the Play Store, or needs update-authenticity guarantees for a genuinely
adversarial threat model, this decision must be revisited with a real,
privately-held signing key — at which point this ADR should be marked
superseded, not edited.

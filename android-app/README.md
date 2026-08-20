# Bot Server — Android app

Kotlin + Jetpack Compose + Material 3 native app for pairing with a running
BotServer instance over Tailscale (see `../docs/mobile-access.md`). Mirrors
the desktop dashboard's Chat, Sessions, Jobs, and Bots tabs, plus QR-code
pairing and push notifications for new inbound messages.

## Building

Requires Android SDK (compileSdk 35), JDK 17.

```
./gradlew assembleDebug
```

Output: `app/build/outputs/apk/debug/app-debug.apk`.

## Before push notifications will actually work: replace `google-services.json`

`app/google-services.json` checked into this repo is a **structurally-valid
placeholder** — it lets the project build (the `google-services` Gradle
plugin validates the file's shape at build time) but contains no real
Firebase project, so `FirebaseMessaging` will never successfully register a
token against it. Real push notifications need:

1. Create a free project at https://console.firebase.google.com.
2. Add an Android app to it with package name `com.botserver.mobile`.
3. Download the real `google-services.json` it gives you and replace
   `app/google-services.json` in this repo with it.
4. On the BotServer side, generate a service account key for that same
   Firebase project (Project settings → Service accounts → Generate new
   private key), save the JSON somewhere on the server machine, and set
   `FCM_SERVICE_ACCOUNT_JSON=<path to that file>` in BotServer's `.env`.
   `bot/push.py` is a no-op until this is set — nothing breaks if you skip
   it, you just won't get push notifications, only the app's normal 2s
   chat polling while it's open.

## Architecture

- `data/` — Retrofit `ApiService` (mirrors the dashboard REST API in
  `bot/dashboard/server.py`), repositories per feature, `CredentialStore`
  (Keystore-backed pairing credentials via EncryptedSharedPreferences).
- `di/` — Hilt module providing a Retrofit client whose base URL is
  resolved fresh per-request from `CredentialStore` (see
  `NetworkModule.kt`'s `DynamicHostInterceptor` — the paired host can
  change if you re-pair, unlike a normal fixed-base-URL Retrofit setup).
- `ui/` — one package per screen (`pairing/`, `chat/`, `sessions/`,
  `jobs/`, `bots/`), plus `nav/HomeScreen.kt` for the bottom-nav shell and
  `theme/` for Material 3 dynamic color.
- `push/FcmService.kt` — `FirebaseMessagingService` subclass; registers
  refreshed tokens and shows a local notification for each inbound push,
  deep-linking back into the app on tap.

## Known gaps (honest, not hidden)

- **Not verified on a real device or emulator.** The build environment
  this was developed in has the Android SDK/Gradle/JDK but no physical
  phone and no pre-existing AVD image with `avdmanager` available to
  create one — `./gradlew assembleDebug` succeeding is real, strong
  evidence the code is correct, but it is not the same as having watched
  it run. Install the debug APK on a real device and pair it against a
  running BotServer instance before relying on it.
- **QR pairing needs a real camera** — inherently untestable in this
  build environment regardless of an emulator, since emulator camera
  support is its own separate can of worms. The manual host/key entry
  fallback on the pairing screen exists specifically so pairing still
  works without ever exercising the camera path, and is the one to test
  first.
- **The `botserver://pair` deep link is parsed but not yet wired** to
  `MainActivity`'s launch intent (see the `TODO` there) — QR scan is the
  fully-wired primary path.

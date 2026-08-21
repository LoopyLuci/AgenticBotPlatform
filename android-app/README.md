# Bot Server — Android app

Kotlin + Jetpack Compose + Material 3 native app for pairing with a running
BotServer instance over Tailscale (see `../docs/mobile-access.md`). Mirrors
the desktop dashboard's Chat, Support Bot, Sessions, Jobs, and Bots tabs
(seven bottom-nav tabs total), plus QR-code pairing and push notifications
for new inbound messages. Every chat composer (Chat and Support) pops the
same "/" slash-command autocomplete menu the desktop app has, and the
Support tab talks to the same local, dependency-free Support Bot model
described in `../docs/support-bot.md` — nothing mobile-specific about its
intelligence, it's the identical server-side engine over
`/api/support-bot/ask` and `/api/support-bot/confirm`.

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

- **Verified on a real device via `adb`** — install, launch, navigate the
  Bots/Support/Chat tabs, use the slash-command menu, and a genuine
  round-trip (`/status` typed on the phone, executed by the real server,
  correct live status rendered back) have all been confirmed against a
  physical phone, not just a passing `./gradlew assembleDebug`.
- **QR pairing needs a real camera** — inherently untestable via `adb`
  alone, since it depends on actually pointing the camera at a QR code.
  The manual host/key entry fallback on the pairing screen exists
  specifically so pairing still works without ever exercising the camera
  path — test that path first if you haven't scanned a real code yet.
- **The `botserver://pair` deep link is parsed but not yet wired** to
  `MainActivity`'s launch intent (see the `TODO` there) — QR scan is the
  fully-wired primary path.

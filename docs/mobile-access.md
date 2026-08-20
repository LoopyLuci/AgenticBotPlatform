# Mobile access — pairing the Android app over the internet

BotServer's dashboard API binds to `127.0.0.1` by default and is protected
by a single `DASHBOARD_TOKEN`. To let the Android app reach it from
anywhere (not just the same machine), you need two things: the server
reachable from your phone, and a mobile API key it can authenticate with.
This doc covers the first part — key generation is in the dashboard's
**Mobile** tab.

## Why Tailscale, not a public port

For a personal, single-user setup, [Tailscale](https://tailscale.com) is
the recommended way to reach BotServer remotely: it creates a private,
encrypted (WireGuard) network between your devices, so your phone reaches
the server machine at a stable address exactly as if it were on the same
LAN — no public port ever opens on your router, no TLS certificate to
provision or renew, and no third party can even see the server exists.
This is meaningfully simpler and safer than a classic reverse-proxy +
domain + TLS setup, which is really only worth the extra operational
burden if you need *non-Tailscale* clients (arbitrary third parties, no
client software) to reach the API — not the case here.

## Setup

1. **Install Tailscale on the machine running BotServer** (Windows):
   download from https://tailscale.com/download, sign in with the account
   you'll use on your phone too.
2. **Install Tailscale on your phone** (Android/iOS), sign into the same
   account. The two devices now share a private `100.x.y.z` address space
   and, usually, MagicDNS hostnames like `your-pc-name.your-tailnet.ts.net`.
3. **Find the server machine's tailnet address** — open the Tailscale app
   or run `tailscale ip` on the server machine. Either the `100.x.y.z` IP
   or the MagicDNS hostname works.
4. **Bind BotServer to be reachable from the tailnet interface**, not just
   loopback. Set in `.env`:
   ```
   DASHBOARD_HOST=0.0.0.0
   ```
   This makes the dashboard listen on every network interface the machine
   has, including the Tailscale one — reachability from *outside* the
   tailnet is still governed by your normal router/firewall (nothing
   changes there). **Use `0.0.0.0`, not the Tailscale interface's specific
   IP** — the desktop Tauri shell's own CSP only allows it to reach
   `127.0.0.1`, so binding to just the tailnet IP makes the desktop app
   itself lose connectivity to its own dashboard (confirmed the hard way).
   Regular-LAN reachability under `0.0.0.0` is bounded by the same
   `DASHBOARD_TOKEN`/mobile-key auth every sensitive route already
   requires.
5. **Generate a mobile key** in the dashboard's Mobile tab, entering the
   tailnet host:port (e.g. `your-pc-name.your-tailnet.ts.net:8787`) so the
   QR code embeds the right address. Scan it with the Android app's
   pairing screen.
6. **Restart BotServer** for the `DASHBOARD_HOST` change to take effect.

## One-click build, install & pair (desktop app, on the dev machine)

If you're running the desktop app on the same machine the Android project
lives on (this repo's `android-app/`, with the Android SDK installed —
`android-app/local.properties`'s `sdk.dir=` or `ANDROID_HOME` pointing at
it), the Mobile tab has an **Android app** card that does the manual steps
below for you: builds a debug APK with Gradle, installs it on a device
connected over USB (enable USB debugging in Developer Options first, and
accept the authorization prompt on the phone), and pairs it automatically
by firing the same `botserver://pair` link the QR encodes directly on the
device via `adb`. Pick the device from the dropdown (refresh if it's not
listed) and click **Build, install & pair**.

This only works on that one machine — it's not something a phone-only user
or a stranger's install can use, since it needs the Android build toolchain
and a USB/adb connection to the device. Everything below (manual build,
manual QR scan) still works everywhere else and is the fallback.

## Push notifications

Optional, on top of the above. When enabled, the phone gets a real
notification the moment a bot receives a new inbound message — not just
whatever the app picks up on its next 2-second chat poll while it's open.

See `android-app/README.md`'s "Before push notifications will actually
work" section for the full setup (a free Firebase project, replacing the
checked-in placeholder `google-services.json`, and setting
`FCM_SERVICE_ACCOUNT_JSON` in BotServer's `.env`). Until that's done,
`bot/push.py` stays a harmless no-op — nothing else about the app or
server depends on it.

## What changed in the app for this

- CORS (`bot/dashboard/server.py`) was tightened from wide-open to just
  the origins actually in use (the Tauri desktop shell, and
  `127.0.0.1`/`localhost` for local browser access) — this only matters
  for browser-based clients; the native Android app sends no `Origin`
  header, so it's unaffected either way.
- No TLS/certificate code was added — Tailscale's own tunnel is already
  the encrypted transport, so plain HTTP between BotServer and the app is
  fine over a tailnet connection (it would not be fine over the open
  internet, which is exactly what this setup avoids ever doing).

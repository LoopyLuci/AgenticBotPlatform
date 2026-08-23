# Connecting Claude Desktop and Hermes Agent — a complete setup guide

Bot Server can drive two different local AI engines: **Claude Desktop**
and **Hermes Agent**. This is the one place that walks through setting up
either (or both) end to end, including the one mistake that will actually
break things if you skip it. Written so both a human and an agent picking
this repo up cold can follow it without any other context.

If you'd rather do this conversationally than read a doc, the Support Bot
now has intents for most of the checks below — see
[docs/support-bot.md](support-bot.md#claudehermes-connection-setup). Ask it
`"check claude desktop setup"` or `"check hermes setup"` from the desktop
dashboard's Support tab (or the Android app's Support tab) and it will run
the same checks this doc describes, in plain English.

## The three things you can connect, and what each one is

| | What it is | Which Bot Server backend uses it | Needed for |
|---|---|---|---|
| **Claude Desktop** | Anthropic's official Windows/macOS app | `ui` (drives the real window via `pywinauto`) | Bot instances that route through `ui` |
| **Hermes Agent (CLI/gateway)** | A separate, open-source AI agent CLI | `hermes_cli` (one-shot `hermes -z`) and `hermes_gateway` (persistent `hermes serve --isolated` process) | Bot instances that route through either Hermes backend |
| **Hermes Desktop** | Hermes's own Electron GUI app (`hermes desktop`) | **Nothing** — it is not a Bot Server backend at all | Your own direct, interactive use of Hermes; entirely independent of Bot Server |

The distinction in that last row matters: unlike Claude Desktop (which the
`ui` backend actively drives via UI automation), Hermes Desktop is just a
separate app you can open and use on your own — Bot Server never touches
it, clicks in it, or reads from it. You can run it side-by-side with Bot
Server with zero interaction between the two, as long as you avoid the
token conflict described below.

## Setting up Claude Desktop (for the `ui` backend)

1. Install Claude Desktop from Anthropic (Windows or macOS — the `ui`
   backend isn't available on Linux since `pywinauto` itself is
   Windows-only; see the main [README](../README.md#notes-on-the-ui-backend)).
2. Sign in once, normally, so it's a working, logged-in install.
3. Bot Server auto-detects the install path. If detection fails, set
   `CLAUDE_DESKTOP_EXE` in `.env` to the full path of the executable
   (Control Center → Environment → **Edit .env contents**, or the setup
   wizard).
4. Verify: Control Center's **Connections & Telemetry** card shows
   `backend · ui` readiness, or ask the Support Bot `"check claude
   desktop setup"` / run `/status` — either way you want to see `ui:
   ready`, not `ui: not set up (...)`.
5. To actually launch/stop/restart the app from Bot Server: the
   dashboard's **Process Controls** card, `/start_desktop` /
   `/stop_desktop` / `/restart_desktop` from any chat platform, or the
   Support Bot ("start claude desktop").

## Setting up Hermes Agent (for `hermes_cli` / `hermes_gateway`)

1. Install Hermes Agent per its own project's instructions — Bot Server
   doesn't install or manage the Hermes installation itself, only calls
   the `hermes` binary once it's on `PATH`.
2. Run `hermes setup` (first-time configuration) and `hermes model` to
   pick a model/provider and complete any auth (API key, OAuth login,
   etc., depending on the provider you choose).
3. Verify Hermes itself is healthy, independent of Bot Server:
   ```powershell
   hermes status
   ```
   Look for a real model/provider under **Environment**, and a live
   entry under **API Keys** or **Auth Providers** for whatever provider
   you configured. `hermes doctor` gives a deeper diagnostic pass if
   something looks wrong.
4. Verify Bot Server sees it: Control Center's **Connections & Telemetry**
   card should show `backend · hermes_cli` and `backend · hermes_gateway`
   as ready (this just checks that `hermes` resolves on `PATH` — it does
   **not** independently verify auth, so step 3 above is still worth
   doing on its own). The Support Bot's `"check hermes setup"` does both
   checks in one message.
5. `hermes_gateway` additionally needs its own port free
   (`backends.hermes_gateway.port` in `config/backends.yaml`, default
   `8799`) — Bot Server spawns and owns `hermes serve --isolated` on that
   port itself; you don't need to start anything manually for it.

### A real gotcha: `hermes_cli` can hang

`hermes -z "<prompt>"` calls can occasionally hang far past Bot Server's
configured timeout (`timeouts.cli` in `config/backends.yaml`, default
`60`s) instead of failing cleanly — observed during this project's own
testing against a slow provider. Bot Server's own timeout still protects
you (the job fails cleanly instead of hanging Bot Server itself), but if
you're troubleshooting from a raw terminal and a bare `hermes -z "..."`
call seems stuck with no output at all for well over a minute, that's a
known rough edge in Hermes itself, not a Bot Server bug — kill the process
and retry, and consider `hermes doctor` if it keeps happening.

## Opening Hermes Desktop (optional, independent of Bot Server)

```bash
hermes desktop
```

Builds (first run) and launches Hermes's own Electron chat app. This is
entirely separate from anything Bot Server does — safe to leave running
alongside Bot Server, with one exception below.

## ⚠️ The one thing that will actually break: shared platform tokens

Hermes Agent has its **own**, completely independent Telegram/Discord/
Slack/etc. integration (`hermes gateway run` / `hermes gateway start`),
configured through Hermes's own `.env` — separate from anything in this
repo. If you give Hermes's own gateway the **same** bot token as a Bot
Server bot instance, both processes will try to long-poll that platform
with the same credential at the same time, and you'll see errors like:

```
telegram.error.Conflict: Conflict: terminated by other getUpdates request;
make sure that only one bot instance is running
```

This is exactly what happened during this project's own development —
Hermes's `.env` had `TELEGRAM_BOT_TOKEN` set to the same token as a real
Bot Server "Hermes Telegram" bot instance, and Hermes also had a Windows
login item (`Hermes_Gateway.vbs`) that auto-starts its gateway on every
boot. The fix is always the same: **pick exactly one owner per token.**
Since Bot Server bot instances are meant to be the sole platform
connection (Hermes is "purely a backend engine behind them" — see the
main README's [Hermes Agent backends](../README.md#hermes-agent-backends)
section), that owner should almost always be Bot Server.

### How to check whether you have this problem

```powershell
# 1. List every token Bot Server's bot instances own:
.\.venv\Scripts\python.exe -c "from bot import bot_instances, db; db.init_db(); [print(i['name'], i['credentials'].get('bot_token')) for i in bot_instances.list_instances()]"

# 2. Check what Hermes's own gateway is configured to use:
Select-String -Path "$env:LOCALAPPDATA\hermes\.env" -Pattern "TELEGRAM_BOT_TOKEN|DISCORD_BOT_TOKEN|SLACK_BOT_TOKEN"
```

If any token appears in both outputs, you have the conflict. The Support
Bot's `"check hermes setup"` intent runs this same comparison for you and
tells you directly if it finds an overlap.

### How to fix it

Comment out (or remove) the conflicting platform's lines in Hermes's own
`.env` (typically `C:\Users\<you>\AppData\Local\hermes\.env` on Windows)
— e.g.:

```diff
-TELEGRAM_BOT_TOKEN=8047927629:AAEh...
-TELEGRAM_ALLOWED_USERS=...
-TELEGRAM_HOME_CHANNEL=...
+#TELEGRAM_BOT_TOKEN=8047927629:AAEh...
+#TELEGRAM_ALLOWED_USERS=...
+#TELEGRAM_HOME_CHANNEL=...
```

Leave any *other* platform Hermes owns exclusively (e.g. Discord, if Bot
Server has no Discord bot instance using that same token) untouched —
this is a per-token fix, not "disable Hermes's whole gateway." If you'd
rather disable Hermes's gateway auto-start entirely instead of editing
its `.env`, remove or rename its Windows login item:
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Hermes_Gateway.vbs`.

Verify the fix by running `hermes gateway status` (should show no
process, or a process that no longer claims the freed token) and
confirming `bot.log` stops showing `Conflict: terminated by other
getUpdates request` after Bot Server restarts its own polling.

## Quick verification checklist

- [ ] `hermes status` (or `hermes doctor`) shows a real model/provider and
      valid auth, independent of Bot Server.
- [ ] Control Center's Connections & Telemetry card (or Support Bot
      `"status"`) shows `ready` for every backend you actually use.
- [ ] No platform token appears in both a Bot Server bot instance's
      credentials *and* Hermes's own `.env`.
- [ ] `bot.log` shows no `Conflict: terminated by other getUpdates
      request` errors after a restart.
- [ ] (Optional) `hermes desktop` opens Hermes's own chat app cleanly,
      independent of anything Bot Server is doing.

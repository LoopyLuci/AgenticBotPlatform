# Connecting Claude Desktop and Hermes Agent — a complete setup guide

Agentic Bot Platform can drive two different local AI engines: **Claude Desktop**
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

| | What it is | Which Agentic Bot Platform backend uses it | Needed for |
|---|---|---|---|
| **Claude Desktop** | Anthropic's official Windows/macOS app | `ui` (drives the real window via `pywinauto`) | Bot instances that route through `ui` |
| **Hermes Agent (CLI/gateway)** | A separate, open-source AI agent CLI | `hermes_cli` (one-shot `hermes -z`) and `hermes_gateway` (persistent `hermes serve --isolated` process) | Bot instances that route through either Hermes backend |
| **Hermes Desktop** | Hermes's own Electron GUI app (`hermes desktop`) | **Nothing** — it is not a Agentic Bot Platform backend at all | Your own direct, interactive use of Hermes; entirely independent of Agentic Bot Platform |

The distinction in that last row matters: unlike Claude Desktop (which the
`ui` backend actively drives via UI automation), Hermes Desktop is just a
separate app you can open and use on your own — Agentic Bot Platform never touches
it, clicks in it, or reads from it. You can run it side-by-side with Bot
Server with zero interaction between the two, as long as you avoid the
token conflict described below.

## Setting up Claude Desktop (for the `ui` backend)

1. Install Claude Desktop from Anthropic (Windows or macOS — the `ui`
   backend isn't available on Linux since `pywinauto` itself is
   Windows-only; see the main [README](../README.md#notes-on-the-ui-backend)).
2. Sign in once, normally, so it's a working, logged-in install.
3. Agentic Bot Platform auto-detects the install path. If detection fails, set
   `CLAUDE_DESKTOP_EXE` in `.env` to the full path of the executable
   (Control Center → Environment → **Edit .env contents**, or the setup
   wizard).
4. Verify: Control Center's **Connections & Telemetry** card shows
   `backend · ui` readiness, or ask the Support Bot `"check claude
   desktop setup"` / run `/status` — either way you want to see `ui:
   ready`, not `ui: not set up (...)`.
5. To actually launch/stop/restart the app from Agentic Bot Platform: the
   dashboard's **Process Controls** card, `/start_desktop` /
   `/stop_desktop` / `/restart_desktop` from any chat platform, or the
   Support Bot ("start claude desktop").

## Setting up Hermes Agent (for `hermes_cli` / `hermes_gateway`)

1. Install Hermes Agent per its own project's instructions — Agentic Bot Platform
   doesn't install or manage the Hermes installation itself, only calls
   the `hermes` binary once it's on `PATH`.
2. Run `hermes setup` (first-time configuration) and `hermes model` to
   pick a model/provider and complete any auth (API key, OAuth login,
   etc., depending on the provider you choose).
3. Verify Hermes itself is healthy, independent of Agentic Bot Platform:
   ```powershell
   hermes status
   ```
   Look for a real model/provider under **Environment**, and a live
   entry under **API Keys** or **Auth Providers** for whatever provider
   you configured. `hermes doctor` gives a deeper diagnostic pass if
   something looks wrong.
4. Verify Agentic Bot Platform sees it: Control Center's **Connections & Telemetry**
   card should show `backend · hermes_cli` and `backend · hermes_gateway`
   as ready (this just checks that `hermes` resolves on `PATH` — it does
   **not** independently verify auth, so step 3 above is still worth
   doing on its own). The Support Bot's `"check hermes setup"` does both
   checks in one message.
5. `hermes_gateway` additionally needs its own port free
   (`backends.hermes_gateway.port` in `config/backends.yaml`, default
   `8799`) — Agentic Bot Platform spawns and owns `hermes serve --isolated` on that
   port itself; you don't need to start anything manually for it.

### A real gotcha: `hermes_cli` can hang

`hermes -z "<prompt>"` calls can occasionally hang far past Agentic Bot Platform's
configured timeout (`timeouts.cli` in `config/backends.yaml`, default
`60`s) instead of failing cleanly — observed during this project's own
testing against a slow provider. Agentic Bot Platform's own timeout still protects
you (the job fails cleanly instead of hanging Agentic Bot Platform itself), but if
you're troubleshooting from a raw terminal and a bare `hermes -z "..."`
call seems stuck with no output at all for well over a minute, that's a
known rough edge in Hermes itself, not a Agentic Bot Platform bug — kill the process
and retry, and consider `hermes doctor` if it keeps happening.

## Opening Hermes Desktop (optional, independent of Agentic Bot Platform)

```bash
hermes desktop
```

Builds (first run) and launches Hermes's own Electron chat app. This is
entirely separate from anything Agentic Bot Platform does — safe to leave running
alongside Agentic Bot Platform, with one exception below.

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
Agentic Bot Platform "Hermes Telegram" bot instance, and Hermes also had a Windows
login item (`Hermes_Gateway.vbs`) that auto-starts its gateway on every
boot. The fix is always the same: **pick exactly one owner per token.**
Since Agentic Bot Platform bot instances are meant to be the sole platform
connection (Hermes is "purely a backend engine behind them" — see the
main README's [Hermes Agent backends](../README.md#hermes-agent-backends)
section), that owner should almost always be Agentic Bot Platform.

### How to check whether you have this problem

```powershell
# 1. List every token Agentic Bot Platform's bot instances own:
.\.venv\Scripts\python.exe -c "from bot import bot_instances, db; db.init_db(); [print(i['name'], i['credentials'].get('bot_token')) for i in bot_instances.list_instances()]"

# 2. Check what Hermes's own gateway is configured to use:
Select-String -Path "$env:LOCALAPPDATA\hermes\.env" -Pattern "TELEGRAM_BOT_TOKEN|DISCORD_BOT_TOKEN|SLACK_BOT_TOKEN"
```

If any token appears in both outputs, you have the conflict. The Support
Bot's `"check hermes setup"` intent runs this same comparison for you and
tells you directly if it finds an overlap.

### How to fix it

Two answers, depending on which owner you actually want. If you want
AgenticBotPlatform to own the bot, comment the token out of Hermes's `.env` as
described below. If you want to keep talking to Hermes on Telegram and run
AgenticBotPlatform beside it, **do nothing** — see the next section, which is
the arrangement the rest of this doc supports.

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
getUpdates request` after Agentic Bot Platform restarts its own polling.

## Agentic Bot Platform and a Hermes Telegram gateway side by side

If you *do* want to keep talking to Hermes directly on Telegram — through
Hermes's own gateway, with its Windows login item starting it at every boot —
and run AgenticBotPlatform's own bots alongside it, you don't have to give
either side up. A Telegram bot token can be long-polled by exactly one
program, and AgenticBotPlatform now knows that.

### Who owns the token, and how Agentic Bot Platform decides

Before any Telegram bot instance starts polling, AgenticBotPlatform asks
Hermes the same question `hermes gateway status` asks, reading Hermes's own
files in every configured `HERMES_HOME` (the default one from `HERMES_HOME`,
plus every per-instance `hermes_home`):

| Hermes's file | What AgenticBotPlatform reads it for |
| --- | --- |
| `<HERMES_HOME>/.env` | an uncommented `TELEGRAM_BOT_TOKEN` |
| `<HERMES_HOME>/config.yaml` | `platforms.telegram.enabled` (an explicit `false` means Hermes will not serve Telegram; absent means Hermes's own rule — credentials in `.env` enable it) |
| `<HERMES_HOME>/gateway.pid`, `gateway_state.json` | the gateway's pid, which must still be alive **and** still look like a `hermes gateway run` process |

If that token is one a running Hermes gateway is already serving, AgenticBotPlatform
does not poll it at all. The bot's card in the dashboard reads **Served by
Hermes** and says `served by the Hermes gateway (<home>)`; the same text is in
the API (`served_by` on `/api/bots`) and in `abp hermes instances`. Tokens are
only ever compared as a sha256 digest — nothing logs or returns the token.

That instance stays unpolled for good, even after you stop the gateway: taking
a token over is opt-in per instance, with `takeover_when_gateway_down: true`
(Bots tab / `abp bots edit --takeover-when-gateway-down true`), and the card
then explains itself instead of looking like a bot that needs pressing Start.

The one instance this never applies to is one whose backend is `hermes_gateway`
and whose `hermes_home` *is* the home owning the token: that instance is the
gateway, AgenticBotPlatform manages it, and it keeps polling exactly as before.

### If something else polls your token anyway

If a second poller appears that AgenticBotPlatform can't see (a second
AgenticBotPlatform, a `python -m telegram` script, an old gateway process),
Telegram answers `getUpdates` with HTTP 409 and python-telegram-bot reports
`telegram.error.Conflict`. python-telegram-bot's default is to log it and retry
forever, backing off to 30 s — which just knocks the other poller off in turn,
forever. AgenticBotPlatform instead stops that one instance, records
`another program is polling this bot (409 Conflict)` as its `last_error`, and
leaves it stopped until you start it again. There is no retry storm, and the
reason survives an AgenticBotPlatform restart.

### Driving the gateway from here

Everything the gateway can do from a terminal, the dashboard API
(`/api/hermes/gateway...`, `POST /api/hermes/ask`) and the CLI do from here:

```
abp hermes status --json        # parsed `hermes gateway status` (pids, service, raw text)
abp hermes list                 # every Hermes profile and whether its gateway is running
abp hermes start                # via Hermes's OWN login-item launcher - see below
abp hermes stop | restart
abp hermes logs --lines 200     # tail of <HERMES_HOME>/logs/gateway*.log
abp hermes instances            # which AgenticBotPlatform bots are served by Hermes
abp hermes ask "summarise today's errors"     # one-shot `hermes -z`
```

`abp hermes start` deliberately does **not** just spawn a child process.
`hermes gateway status` warns that a gateway started from a shell inside a
Windows Job Object gets killed when that shell exits (Hermes's own #91675), and
AgenticBotPlatform is very often itself inside one (Tauri/Electron, Windows
Terminal). So `start` reuses the launcher Hermes already installs for its login
item — `<HERMES_HOME>/gateway-service/Hermes_Gateway.vbs`, run through `cscript`
hidden and non-blocking — which starts the gateway outside AgenticBotPlatform's
process tree, exactly as it does at logon. Everything is windowless: no console
window ever appears on your desktop.

`abp hermes ask` (and `POST /api/hermes/ask`) is `hermes -z`, a fresh
per-invocation process. It never touches `getUpdates`, so AgenticBotPlatform's
own channels can use Hermes as a backend *while* the gateway keeps serving
Telegram — no token contention, no second gateway.

### Checking it

```powershell
# who owns what, with no tokens printed
abp hermes status --json
abp hermes instances
abp bots list          # the "served_by" column on any bot a Hermes gateway serves
```

## Quick verification checklist

- [ ] `hermes status` (or `hermes doctor`) shows a real model/provider and
      valid auth, independent of Agentic Bot Platform.
- [ ] Control Center's Connections & Telemetry card (or Support Bot
      `"status"`) shows `ready` for every backend you actually use.
- [ ] No platform token is polled by two programs: either no token appears in
      both a Agentic Bot Platform bot instance's credentials *and* Hermes's own
      `.env`, or the instance one owns is marked `served by the Hermes
      gateway` (`abp hermes instances`).
- [ ] `bot.log` shows no `Conflict: terminated by other getUpdates
      request` errors after a restart.
- [ ] (Optional) `hermes desktop` opens Hermes's own chat app cleanly,
      independent of anything Agentic Bot Platform is doing.

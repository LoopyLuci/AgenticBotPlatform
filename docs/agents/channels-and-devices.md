# New channels, paired-phone nodes, voice and the canvas

Nothing here has been run against the real service or app it talks to. Every part was tested against fakes written for
the tests; where a real counterpart could be checked locally it says so.

## Channels

Add them from the dashboard, the desktop app or the terminal UI ("Add a bot" -> platform); their credential fields and
setup steps come from `/api/platform-guides`. Only the listed senders are answered; anything else is audited
(`unauthorized_attempt`) and dropped without a reply.

| Channel | How it connects | Notes |
| --- | --- | --- |
| **E-mail** | IMAP polling for the bot's inbox, SMTP for replies | A From address is easy to forge, so a mail is only answered if your provider recorded a passing SPF, DKIM or DMARC result (`email.require_authentication`, on by default), and it ignores automatic replies, bulk mail, its own mail, and more than 20 replies an hour to one sender. Attachments are ignored. App passwords only: no OAuth. |
| **SMS** (Twilio) | Twilio posts each text to `/webhooks/sms`; replies go through Twilio's REST API | Requests are verified with Twilio's `X-Twilio-Signature` (the algorithm reproduces the example in Twilio's own documentation); behind a proxy set `sms.public_url`. Replies cost money per segment. |
| **Signal** | A [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) bridge you run; ABP polls it | The bridge holds the Signal keys and sees messages in the clear. Group messages are ignored. `json-rpc` mode of the bridge is not supported. |
| **iMessage** | A [BlueBubbles](https://bluebubbles.app) server on a Mac you own; it posts to `/webhooks/bluebubbles?token=...` | Needs a Mac signed in to iMessage. The token is compared in constant time. Group chats ignored. |
| **Google Chat** | A Chat app with an HTTP endpoint posts each event to `/webhooks/googlechat`. Replies are posted through the Chat API as your service account, in the message's thread | Every request's bearer token is verified as Google documents: for the "Project number" audience, a JWT from `chat@system.gserviceaccount.com` checked against its published certificates; for "HTTP endpoint URL", a Google ID token whose email is that account. Anything else gets 401. People @mention the bot in spaces. The Workspace add-on variant of a Chat app is refused with a message saying so. |
| **Microsoft Teams** | An Azure Bot's messaging endpoint is `/webhooks/teams`. Replies are posted to the Bot Connector with an Entra ID token (client credentials) | Verified as Microsoft documents: signature against login.botframework.com's keys, issuer `https://api.botframework.com`, audience the App ID, the token's `serviceUrl` equal to the activity's, and channel endorsements (403 without one). A typing indicator shows while the agent works. Single-tenant (tenant ID) and multi-tenant bots are both handled. |
| **App only** (`app`) | No external platform at all — reached only through `POST /api/chat/send-to-bot`, the same route the desktop app, the Android app and the CLI/TUI already use | Needs no credentials and no allowed-user-id list: access is whoever can already authenticate to the dashboard API (`DASHBOARD_TOKEN`, or a paired device's own key). Has no live connection to start, stop or crash — `bot/platform_supervisor.py`'s `start_instance` is a no-op for it. |

**Google Chat and Teams were not run against real accounts:** there is no Google Workspace or Azure tenant here. They were
tested end to end against local stand-ins for Google's and Microsoft's servers (`tests/test_googlechat_teams.py`), with real
RS256 tokens, a token endpoint that checks the service account's signed assertion, and captured replies. The stand-ins
follow the documented formats, which were re-read from the official pages when this was written.

Those tests reject every forged request: no token, the wrong audience, issuer, signature, key or algorithm, an expired
token (5 minutes of clock skew is allowed), a mismatched `serviceUrl`, and a missing endorsement. They also cover a key
rotated in since the last fetch.

## Paired phones as nodes

A paired device can be asked, by the agent, for a photo (`camera.snap`), the screen (`screen.capture`), its location
(`location.get`), its clipboard (`clipboard.read`), or to show a notification (`notify.show`). Tools: `node_list` and
`node_invoke`. **Consent per capability starts at `deny`**; the dashboard token sets it to `device` (the phone asks its own
user each time) or `allow`; a device cannot grant itself anything and the agent cannot change it. `node_invoke` is not
read-only, so it is approved like any other action, and whatever comes back is untrusted content that taints the session.

The server half is complete: registration, long-poll delivery, results, consent, push wake-up. **The Android app does not
implement it yet**, so no real phone has answered a command. `python -m abp_node` is a reference node with canned answers,
and the wire format is in the `bot/nodes.py` docstring; both are what an app implementation starts from.

## Voice

`voice:` in `config/backends.yaml`, off until configured, no model bundled. Speech to text through any OpenAI-compatible
`/audio/transcriptions` endpoint (Groq's free tier includes Whisper; these calls are counted against its allowance like
any model call - see [models.md](models.md)) or a command you supply (whisper.cpp, for example). Text to speech through
an OpenAI-compatible `/audio/speech` endpoint, Windows' built-in speech (verified here: it produced a real WAV file), or a
command (Piper, espeak).

**Telegram, Discord and Slack all run this one pipeline** (`bot/voice.py`, `bot/platforms/_voice.py` for the two
adapters) off the same `voice:` block - there is no second implementation and no per-platform setting. A voice message
is transcribed, the reply begins "Heard: ..." so a wrong transcript is visible, and then it is handled exactly like
typed text: same allow-list, same slash commands, same permissions, same approvals. With `reply_with_voice: true` the
answer is also sent back as audio.

Where the audio comes from differs, and is the only thing that does:

| Channel | Fetched from | Size cap | Reply as audio |
|---|---|---|---|
| Telegram | the Bot API's own `getFile`, with the bot's token | `media.file_size`, checked before the download | a voice note (`.ogg`) or a file |
| Discord | the attachment's CDN url, with `Authorization: Bot <this bot's token>` | `attachment.size`, checked before the download, and again while the bytes stream | a `discord.File` attachment |
| Slack | `url_private_download` (falling back to `url_private`) with `Authorization: Bearer <this bot's token>` | `file.size`, checked before the download, and again while the bytes stream | `files_upload_v2` |

Audio is never sent to a speech-to-text service you have not configured: with no engine set, the person is told so in
the same words on every channel and the recording stays where it is. The defaults are unchanged and shared by every
channel - transcription off, voice replies off, `max_seconds: 300`. These settings live in `config/backends.yaml` (and
are readable and settable through the generic `/api/config` and `/api/config/set` routes). They are **not**
per-`bot_instances` columns and they have **no** form in the dashboard or the desktop app - Telegram's never had one,
so "the same settings as Telegram" means exactly this one global block, shown wherever it was shown before (the YAML
and the two generic config routes). Making them per instance would mean adding a `bot_instances` column and a form
for all three channels at once; that is not done.

Verified against local stand-ins speaking the real endpoints, with real WAV audio over real HTTP
(`tests/test_platform_voice.py`): the transcript reaching the router as the user's text, the spoken reply produced
(both an OpenAI-compatible endpoint and Windows speech), the size cap, and the no-engine-configured path.
**Not tested against a real Whisper service, Piper, whisper.cpp, a real Discord server or a real Slack workspace.**

## The canvas

The agent calls `canvas_update(name, html)`; you open the link from `POST /api/canvas/{name}/link` and watch it change as the
agent works. The HTML is model-written, so it runs in a sandbox with no dashboard access and no network
(`Content-Security-Policy: sandbox allow-scripts; ... connect-src 'none'`), behind a short-lived signed address. It is a web
page: the desktop and Android apps do not embed it yet.

## Heartbeat

Already existed (`/heartbeat every <interval> <prompt>`, which re-enters a session when idle); nothing new was built.

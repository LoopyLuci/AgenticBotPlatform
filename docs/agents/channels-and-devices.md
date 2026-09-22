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
| **App only** (`app`) | No external platform at all — reached only through `POST /api/chat/send-to-bot`, the same route the desktop app, the Android app and the CLI/TUI already use | Needs no credentials and no allowed-user-id list: access is whoever can already authenticate to the dashboard API (`DASHBOARD_TOKEN`, or a paired device's own key). Has no live connection to start, stop or crash — `bot/platform_supervisor.py`'s `start_instance` is a no-op for it. |

Not built: Google Chat and Microsoft Teams (both need Google/Microsoft sign-in flows that cannot be tested here).

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
command (Piper, espeak). A Telegram voice message is transcribed, the reply begins "Heard: ..." so a wrong transcript is
visible, and then it is handled exactly like typed text (same allow-list, permissions and approvals). With
`reply_with_voice: true` the answer is also sent as audio. Voice on the other channels and in the apps is not done.

## The canvas

The agent calls `canvas_update(name, html)`; you open the link from `POST /api/canvas/{name}/link` and watch it change as the
agent works. The HTML is model-written, so it runs in a sandbox with no dashboard access and no network
(`Content-Security-Policy: sandbox allow-scripts; ... connect-src 'none'`), behind a short-lived signed address. It is a web
page: the desktop and Android apps do not embed it yet.

## Heartbeat

Already existed (`/heartbeat every <interval> <prompt>`, which re-enters a session when idle); nothing new was built.

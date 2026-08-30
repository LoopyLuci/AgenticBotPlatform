"""Per-platform, per-field help text and setup guidance for bot credentials.

Single source of truth for "how do I get a Telegram/Discord/Slack/Matrix/
WhatsApp bot token" prose, shared by the dashboard's Add-a-bot form, the
desktop app (same static assets), and bot/tui/ — all fetch it once via
GET /api/platform-guides instead of each hand-duplicating this text.

Keyed by the DB credential field names bot_instances.py's JSON credentials
blob uses (the same keys bot/validators.py's PLATFORM_TOKEN_VALIDATORS
indexes by) — not the legacy .env field names bot/setup_wizard.py's
PLATFORM_FIELDS uses for the older single-instance path. The prose below
was originally written for that legacy wizard; it's reused here rewritten
against the DB-backed field names rather than duplicated from scratch.
"""

from __future__ import annotations

PLATFORM_GUIDES: dict[str, dict] = {
    "telegram": {
        "label": "Telegram",
        "fields": {
            "bot_token": {
                "label": "Bot token",
                "help": "From @BotFather on Telegram — send /newbot or /mybots.",
            },
        },
        "setup_guide": [
            "Message @BotFather on Telegram, send /newbot, follow the prompts.",
            "Copy the token it gives you into the Bot token field.",
            "Message @userinfobot to get your own numeric Telegram user ID for Allowed user ID(s).",
        ],
    },
    "discord": {
        "label": "Discord",
        "fields": {
            "bot_token": {
                "label": "Bot token",
                "help": "Discord Developer Portal -> your app -> Bot -> Reset Token.",
            },
        },
        "setup_guide": [
            "discord.com/developers/applications -> New Application.",
            "Bot tab -> Reset Token (copy it) -> turn on \"Message Content Intent\" under Privileged Gateway Intents.",
            "OAuth2 -> URL Generator: scope \"bot\", permissions \"Send Messages\" + \"Read Message History\" -> open the generated URL -> invite it to a server you own.",
            "User Settings -> Advanced -> turn on Developer Mode, then right-click your own name anywhere -> Copy User ID.",
        ],
    },
    "slack": {
        "label": "Slack",
        "fields": {
            "bot_token": {
                "label": "Bot token (xoxb-...)",
                "help": "OAuth & Permissions -> Bot User OAuth Token, after installing to workspace.",
            },
            "app_token": {
                "label": "App token (xapp-...)",
                "help": "Socket Mode -> Generate Token and Scopes, with connections:write.",
            },
        },
        "setup_guide": [
            "api.slack.com/apps -> Create New App -> From scratch.",
            "Socket Mode -> enable it -> Generate Token and Scopes, add connections:write -> that's App token.",
            "OAuth & Permissions -> Bot Token Scopes: add chat:write, im:history, im:read -> Install to Workspace -> that's Bot token.",
            "Event Subscriptions -> Subscribe to bot events -> add message.im (and message.channels for channels, not just DMs).",
            "Your profile picture -> \"...\" More -> Copy member ID -> Allowed user ID(s) below.",
        ],
    },
    "matrix": {
        "label": "Matrix",
        "fields": {
            "homeserver": {
                "label": "Homeserver URL",
                "help": "e.g. https://matrix.org",
            },
            "user_id": {
                "label": "Matrix user ID",
                "help": "e.g. @mybot:matrix.org",
            },
            "access_token": {
                "label": "Access token",
                "help": "Element -> Settings -> Help & About -> Advanced -> Access Token, or POST /_matrix/client/v3/login.",
            },
            "device_id": {
                "label": "Device ID (optional)",
                "help": "Leave blank to auto-assign.",
            },
        },
        "setup_guide": [
            "Create or log into a Matrix account for the bot (any homeserver, e.g. matrix.org).",
            "Element -> Settings -> Help & About -> Advanced -> Access Token (or POST /_matrix/client/v3/login) to get an access token.",
            "Invite the bot into an unencrypted room — encrypted rooms aren't supported (no Olm/Megolm store).",
        ],
    },
    "whatsapp": {
        "label": "WhatsApp",
        "fields": {
            "phone_number_id": {
                "label": "Phone Number ID",
                "help": "The numeric Phone Number ID from the Cloud API dashboard, not the phone number itself.",
            },
            "access_token": {
                "label": "Access token",
                "help": "A permanent System User token with the whatsapp_business_messaging permission.",
            },
            "app_secret": {
                "label": "App secret",
                "help": "Settings -> Basic.",
            },
            "verify_token": {
                "label": "Verify token",
                "help": "Pick any string, 6+ characters — you'll enter this same value in Meta's webhook config.",
            },
        },
        "setup_guide": [
            "Create a Meta Business app with the WhatsApp product added.",
            "Copy the Phone Number ID from the Cloud API dashboard.",
            "Generate a permanent System User access token with the whatsapp_business_messaging permission.",
            "Settings -> Basic for the App secret; pick any Verify token string, 6+ characters.",
            "In Meta's App Dashboard -> WhatsApp -> Configuration, set the webhook URL to <your public HTTPS domain>/webhooks/whatsapp with the same verify token — one webhook per App, shared by every WhatsApp instance on this install.",
        ],
    },
}

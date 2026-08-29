"""Shared field validators — format checks for tokens/keys/IDs pasted into
the setup wizard or a bot instance's credential form. Extracted out of
setup_wizard.py so bot_instances.py (DB-backed, per-instance credentials)
and setup_wizard.py (.env-backed, core/legacy fields) validate the exact
same way instead of maintaining two copies of the same regexes.

Every validator returns (ok, message) — message is shown to the user
either way, explaining what's right or wrong.
"""

from __future__ import annotations

import re
from pathlib import Path


def validate_telegram_token(v: str) -> tuple[bool, str]:
    if re.match(r"^\d{6,}:[A-Za-z0-9_-]{30,}$", v):
        return True, "looks like a valid bot token"
    return False, "should look like 123456789:AAExampleTokenFromBotFather"


def validate_anthropic_key(v: str) -> tuple[bool, str]:
    if v.startswith("sk-ant-") and len(v) > 20:
        return True, "looks like a valid Anthropic API key"
    return False, "should start with sk-ant- (from console.anthropic.com/settings/keys)"


def validate_user_ids(v: str) -> tuple[bool, str]:
    parts = [p.strip() for p in v.split(",") if p.strip()]
    if parts and all(p.isdigit() for p in parts):
        return True, f"{len(parts)} user id{'s' if len(parts) != 1 else ''}"
    return False, "should be one or more numeric user IDs, comma-separated"


def validate_dashboard_token(v: str) -> tuple[bool, str]:
    if len(v) >= 16:
        return True, "looks good"
    return False, "should be at least 16 random characters — use Generate"


def validate_desktop_path(v: str) -> tuple[bool, str]:
    if Path(v).exists():
        return True, "found"
    return False, "path doesn't exist yet — will still be saved as-is"


def validate_discord_token(v: str) -> tuple[bool, str]:
    if re.match(r"^[\w-]{20,}\.[\w-]{6,}\.[\w-]{20,}$", v):
        return True, "looks like a valid Discord bot token"
    return False, "should be a bot token from the Developer Portal's Bot tab"


def validate_slack_bot_token(v: str) -> tuple[bool, str]:
    if v.startswith("xoxb-") and len(v) > 20:
        return True, "looks like a valid Slack bot token"
    return False, "should start with xoxb- (Bot User OAuth Token, after installing to workspace)"


def validate_slack_app_token(v: str) -> tuple[bool, str]:
    if v.startswith("xapp-") and len(v) > 20:
        return True, "looks like a valid Slack app token"
    return False, "should start with xapp- (Socket Mode -> Generate Token and Scopes)"


def validate_slack_user_ids(v: str) -> tuple[bool, str]:
    parts = [p.strip() for p in v.split(",") if p.strip()]
    if parts and all(re.match(r"^[UW][A-Z0-9]{6,}$", p) for p in parts):
        return True, f"{len(parts)} user id{'s' if len(parts) != 1 else ''}"
    return False, "should be one or more Slack member IDs (start with U or W), comma-separated"


def validate_matrix_homeserver(v: str) -> tuple[bool, str]:
    if re.match(r"^https?://", v):
        return True, "looks like a valid homeserver URL"
    return False, "should be a full URL, e.g. https://matrix.org"


def validate_matrix_user_id(v: str) -> tuple[bool, str]:
    if re.match(r"^@[^:]+:.+$", v):
        return True, "looks like a valid Matrix user ID"
    return False, "should look like @yourbot:matrix.org"


def validate_matrix_access_token(v: str) -> tuple[bool, str]:
    if len(v) > 10:
        return True, "looks good"
    return False, "should be an access token (Element -> Advanced -> Access Token, or POST /_matrix/client/v3/login)"


def validate_port(v: str) -> tuple[bool, str]:
    if v.isdigit() and 1 <= int(v) <= 65535:
        return True, "valid port"
    return False, "should be a port number between 1 and 65535"


# Per-platform credential validators, keyed the way bot_instances.py's
# CREDENTIAL_FIELDS shape expects — one place both a bot instance's
# create/update path and (indirectly, via the legacy PLATFORM_FIELDS
# validators below) the old setup wizard draw from.
PLATFORM_TOKEN_VALIDATORS = {
    "telegram": {"bot_token": validate_telegram_token},
    "discord": {"bot_token": validate_discord_token},
    "slack": {"bot_token": validate_slack_bot_token, "app_token": validate_slack_app_token},
    "matrix": {
        "homeserver": validate_matrix_homeserver,
        "user_id": validate_matrix_user_id,
        "access_token": validate_matrix_access_token,
    },
}

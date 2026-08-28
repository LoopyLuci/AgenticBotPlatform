"""First-run setup: validates and writes the .env this whole app depends on.

Shared by scripts/setup.py (a terminal wizard that works before anything
else is running — no server, no venv activation ritual, just python) and
bot/dashboard/server.py's /api/setup/* endpoints (the in-GUI wizard the
Tauri app shows automatically when setup isn't complete). Both paths funnel
through check_status()/apply_setup() here, so the validation rules and the
file-patching logic exist in exactly one place.

Writing goes through bot.envfile.write_content(), so every apply is
backed up first, same as a manual dashboard edit.
"""

from __future__ import annotations

import io
import re
import secrets
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import dotenv_values

from bot import envfile
from bot.validators import (
    validate_anthropic_key as _validate_anthropic_key,
    validate_dashboard_token as _validate_dashboard_token,
    validate_desktop_path as _validate_desktop_path,
    validate_discord_token as _validate_discord_token,
    validate_slack_app_token as _validate_slack_app_token,
    validate_slack_bot_token as _validate_slack_bot_token,
    validate_slack_user_ids as _validate_slack_user_ids,
    validate_telegram_token as _validate_telegram_token,
    validate_user_ids as _validate_user_ids,
)


# Core fields: the dashboard itself and (conditionally) the api backend.
# Which messaging platform to use lives in PLATFORM_FIELDS below — none of
# Telegram/Discord/Slack is mandatory here, only "at least one platform is
# fully configured" is, checked via any_platform_configured().
FIELDS: dict[str, dict[str, Any]] = {
    "ANTHROPIC_API_KEY": {
        "required": True,
        "validate": _validate_anthropic_key,
        "label": "Anthropic API key",
        "help": "console.anthropic.com/settings/keys",
    },
    "DASHBOARD_TOKEN": {
        "required": True,
        "validate": _validate_dashboard_token,
        "label": "Dashboard token",
        "help": "Any random string — Generate makes one for you.",
    },
    "CLAUDE_DESKTOP_EXE": {
        "required": False,
        "validate": _validate_desktop_path,
        "label": "Claude Desktop path (optional)",
        "help": "Leave blank to auto-detect at runtime.",
    },
}

FIELD_ORDER = list(FIELDS.keys())

# Messaging platforms — each entirely optional on its own; the app just
# needs at least one of them fully configured. "gate_fields" are the
# fields that must ALL be present and valid for that platform to count as
# usable; setup_guide is shown in the dashboard's Platforms settings,
# reachable any time (not just first-run), per platform.
PLATFORM_FIELDS: dict[str, dict[str, Any]] = {
    "telegram": {
        "label": "Telegram",
        "fields": {
            "TELEGRAM_BOT_TOKEN": {
                "validate": _validate_telegram_token,
                "label": "Bot token",
                "help": "From @BotFather on Telegram — send /newbot or /mybots.",
            },
            "ALLOWED_TELEGRAM_USER_IDS": {
                "validate": _validate_user_ids,
                "label": "Allowed user ID(s)",
                "help": "Message @userinfobot on Telegram to get your numeric ID.",
            },
        },
        "gate_fields": ["TELEGRAM_BOT_TOKEN", "ALLOWED_TELEGRAM_USER_IDS"],
        "setup_guide": [
            "Message @BotFather on Telegram, send /newbot, follow the prompts.",
            "Copy the token it gives you into Bot token below.",
            "Message @userinfobot to get your own numeric Telegram user ID, paste it into Allowed user ID(s).",
        ],
    },
    "discord": {
        "label": "Discord",
        "fields": {
            "DISCORD_BOT_TOKEN": {
                "validate": _validate_discord_token,
                "label": "Bot token",
                "help": "Discord Developer Portal -> your app -> Bot -> Reset Token.",
            },
            "DISCORD_ALLOWED_USER_IDS": {
                "validate": _validate_user_ids,
                "label": "Allowed user ID(s)",
                "help": "Enable Developer Mode, then right-click your name -> Copy User ID.",
            },
        },
        "gate_fields": ["DISCORD_BOT_TOKEN", "DISCORD_ALLOWED_USER_IDS"],
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
            "SLACK_BOT_TOKEN": {
                "validate": _validate_slack_bot_token,
                "label": "Bot token (xoxb-...)",
                "help": "OAuth & Permissions -> Bot User OAuth Token, after installing to workspace.",
            },
            "SLACK_APP_TOKEN": {
                "validate": _validate_slack_app_token,
                "label": "App token (xapp-...)",
                "help": "Socket Mode -> Generate Token and Scopes, with connections:write.",
            },
            "SLACK_ALLOWED_USER_IDS": {
                "validate": _validate_slack_user_ids,
                "label": "Allowed user ID(s)",
                "help": "Your profile picture -> More -> Copy member ID.",
            },
        },
        "gate_fields": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USER_IDS"],
        "setup_guide": [
            "api.slack.com/apps -> Create New App -> From scratch.",
            "Socket Mode -> enable it -> Generate Token and Scopes, add connections:write -> that's App token.",
            "OAuth & Permissions -> Bot Token Scopes: add chat:write, im:history, im:read -> Install to Workspace -> that's Bot token.",
            "Event Subscriptions -> Subscribe to bot events -> add message.im (and message.channels for channels, not just DMs).",
            "Your profile picture -> \"...\" More -> Copy member ID -> Allowed user ID(s) below.",
        ],
    },
}


def generate_dashboard_token() -> str:
    return secrets.token_hex(24)


def current_values() -> dict[str, str]:
    """Values as currently written to disk — not os.environ, which may be
    stale or unset if this runs before anything has loaded the file."""
    content = envfile.read_content()
    return {k: v for k, v in dotenv_values(stream=io.StringIO(content)).items() if v is not None}


def active_backends() -> set[str]:
    """Which backends actually have something routing to them — the global
    config/backends.yaml chain (default, every action_override's backend
    and backup entries) plus every enabled bot instance's own backend and
    action_overrides. Once bot instances exist they're the primary way a
    backend gets used, so this has to look at both, not just the legacy
    global chain, or the readiness panel would show real in-use backends
    as unused. A backend nobody's routing to doesn't need its
    prerequisites configured at all."""
    from bot.config import config

    cfg = config.current
    names = {cfg.get("default_backend", "api")}
    for entry in (cfg.get("action_overrides") or {}).values():
        if entry.get("backend"):
            names.add(entry["backend"])
        names.update(entry.get("backup") or [])

    try:
        from bot import bot_instances

        for inst in bot_instances.list_instances(enabled_only=True):
            if inst.get("backend"):
                names.add(inst["backend"])
            for entry in (inst.get("action_overrides") or {}).values():
                if entry.get("backend"):
                    names.add(entry["backend"])
                names.update(entry.get("backup") or [])
    except Exception:
        pass  # DB not initialized yet (e.g. called very early at boot)

    from bot.router import VALID_BACKENDS

    return names & set(VALID_BACKENDS)


def _api_ready() -> tuple[bool, str]:
    api_key = (current_values().get("ANTHROPIC_API_KEY") or "").strip()
    ok = bool(api_key) and _validate_anthropic_key(api_key)[0]
    return ok, "" if ok else "ANTHROPIC_API_KEY is not set"


def _cli_ready() -> tuple[bool, str]:
    from bot import desktop
    from bot.config import config

    binary = ((config.current.get("backends") or {}).get("cli") or {}).get("binary", "claude")
    ok = desktop.find_cli_path(binary) is not None
    return ok, "" if ok else f"'{binary}' not found on PATH or bundled with Claude Desktop — install/update it below"


def _hermes_cli_ready() -> tuple[bool, str]:
    import shutil

    ok = shutil.which("hermes") is not None
    return ok, "" if ok else "'hermes' not found on PATH — install Hermes Agent"


def _hermes_gateway_ready() -> tuple[bool, str]:
    # Same underlying binary as hermes_cli — the gateway backend spawns its
    # own `hermes serve` process on first use, so "ready" here just means
    # the binary exists to spawn; a real connection failure at first ask()
    # will surface as a per-job error, not a setup-time block.
    return _hermes_cli_ready()


def _ui_ready() -> tuple[bool, str]:
    from bot import desktop

    exe = desktop.find_exe_path()
    ok = bool(exe and Path(exe).exists())
    return ok, "" if ok else "Claude Desktop wasn't found — set CLAUDE_DESKTOP_EXE, or install Claude Desktop"


def _custom_model_ready() -> tuple[bool, str]:
    from bot import providers

    ok = bool(providers.list_providers())
    return ok, "" if ok else "no providers configured in config/providers.yaml"


_READINESS_CHECKS: dict[str, Callable[[], tuple[bool, str]]] = {
    "api": _api_ready,
    "cli": _cli_ready,
    "ui": _ui_ready,
    "hermes_cli": _hermes_cli_ready,
    "hermes_gateway": _hermes_gateway_ready,
    "custom_model": _custom_model_ready,
}


def check_backend_ready(name: str) -> tuple[bool, str]:
    """(ready, reason) for one backend — what the router checks before
    every attempt, so a missing prerequisite is a clear directive instead
    of a raw exception three layers down."""
    fn = _READINESS_CHECKS.get(name)
    return fn() if fn else (True, "")


def backend_readiness() -> dict[str, dict[str, Any]]:
    """All five backends' status at once, for display (the dashboard's
    "available backends" panel) — separate from active_backends(), which
    is about whether anything's routing to them."""
    active = active_backends()
    out = {}
    for name, fn in _READINESS_CHECKS.items():
        ok, reason = fn()
        out[name] = {"ready": ok, "reason": reason or None, "in_use": name in active}
    return out


def _dynamic_required(key: str, active: set[str]) -> bool:
    # DASHBOARD_TOKEN is fixed-required (the dashboard's own security
    # boundary, independent of chat platform). ANTHROPIC_API_KEY depends on
    # whether anything's actually routed to the api backend. CLAUDE_DESKTOP_EXE
    # stays optional even when ui is active, since it auto-detects at
    # runtime either way. Which messaging platform to use lives entirely in
    # PLATFORM_FIELDS/any_platform_configured(), not here.
    if key == "ANTHROPIC_API_KEY":
        return "api" in active
    return FIELDS[key]["required"]


def is_required(key: str) -> bool:
    return _dynamic_required(key, active_backends())


def platform_status() -> dict[str, Any]:
    """Every messaging platform's field status, independent of each other —
    a platform you're not using shows its blank fields as "not set" without
    being flagged invalid, same treatment as an unused backend. "configured"
    is the one flag that matters for whether that platform will actually
    start: all of its gate_fields present and valid."""
    values = current_values()
    out: dict[str, Any] = {}
    for key, spec in PLATFORM_FIELDS.items():
        fields: dict[str, Any] = {}
        for fkey, fspec in spec["fields"].items():
            raw = (values.get(fkey) or "").strip()
            validator: Callable[[str], tuple[bool, str]] = fspec["validate"]
            if not raw:
                fields[fkey] = {
                    "present": False,
                    "valid": True,
                    "message": "not set",
                    "label": fspec["label"],
                    "help": fspec["help"],
                }
            else:
                ok, msg = validator(raw)
                fields[fkey] = {
                    "present": True,
                    "valid": ok,
                    "message": msg,
                    "label": fspec["label"],
                    "help": fspec["help"],
                }
        configured = all(fields[k]["present"] and fields[k]["valid"] for k in spec["gate_fields"])
        out[key] = {
            "label": spec["label"],
            "fields": fields,
            "configured": configured,
            "setup_guide": spec.get("setup_guide", []),
        }
    return out


def any_platform_configured() -> bool:
    return any(p["configured"] for p in platform_status().values())


def apply_platform_fields(values: dict[str, str], actor: str = "dashboard") -> tuple[Optional[Path], dict[str, Any]]:
    """Same shape as apply_setup() but for platform fields (Telegram/
    Discord/Slack tokens and allowlists) — kept separate since they're not
    part of the core FIELDS gate, just each platform's own on/off switch."""
    allowed_keys = {k for spec in PLATFORM_FIELDS.values() for k in spec["fields"]}
    updates = {k: v.strip() for k, v in values.items() if k in allowed_keys and v is not None and v.strip()}
    if not updates:
        return None, platform_status()
    current = envfile.read_content()
    new_content = set_env_vars(current, updates)
    backup = envfile.write_content(new_content, actor=actor)
    return backup, platform_status()


def check_status() -> dict[str, Any]:
    values = current_values()
    active = active_backends()
    fields: dict[str, dict[str, Any]] = {}
    for key, spec in FIELDS.items():
        required = _dynamic_required(key, active)
        raw = (values.get(key) or "").strip()
        validator: Callable[[str], tuple[bool, str]] = spec["validate"]
        if not raw:
            fields[key] = {
                "present": False,
                "valid": not required,
                "message": "not set" if required else "not set (not needed — nothing routes to this backend)",
                "label": spec["label"],
                "help": spec["help"],
                "required": required,
            }
            continue
        ok, msg = validator(raw)
        fields[key] = {
            "present": True,
            "valid": ok,
            "message": msg,
            "label": spec["label"],
            "help": spec["help"],
            "required": required,
        }
    core_ready = all(fields[k]["valid"] for k in FIELDS if fields[k]["required"])
    platforms = platform_status()
    has_bot = False
    try:
        from bot import bot_instances

        has_bot = bool(bot_instances.list_instances(enabled_only=True))
    except Exception:
        pass  # DB not initialized yet (e.g. called very early at boot)
    ready = core_ready and (has_bot or any(p["configured"] for p in platforms.values()))
    return {
        "fields": fields,
        "backends": backend_readiness(),
        "platforms": platforms,
        "ready": ready,
        "env_path": str(envfile.resolve()),
    }


def _line_key(line: str) -> Optional[str]:
    stripped = line.lstrip("#").lstrip()
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", stripped)
    return m.group(1) if m else None


BASE_TEMPLATE = """# Telegram bot token from @BotFather
#TELEGRAM_BOT_TOKEN=

# Your Anthropic API key, used by the "api" backend
ANTHROPIC_API_KEY=

# Comma-separated Telegram numeric user IDs allowed to use the bot.
# Get your own ID by messaging @userinfobot on Telegram.
ALLOWED_TELEGRAM_USER_IDS=

# Shared secret required in the X-Dashboard-Token header for any
# state-changing dashboard request (start/stop/reload/mcp toggle/etc).
# Generate one with: python -c "import secrets; print(secrets.token_hex(24))"
DASHBOARD_TOKEN=

# Dashboard bind address. Keep this on localhost unless you have your
# own reverse proxy + auth in front of it.
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8787

# Optional: override Claude Desktop's install path if auto-detection fails.
# CLAUDE_DESKTOP_EXE=C:\\Users\\you\\AppData\\Local\\AnthropicClaude\\Claude.exe
"""


def set_env_vars(content: str, updates: dict[str, str]) -> str:
    """Patch known KEY=value lines in place — uncommenting a placeholder
    line if that's what's there — append any key that doesn't exist yet,
    and leave every other line (comments, unrelated vars) untouched."""
    base = content if content.strip() else BASE_TEMPLATE
    lines = base.splitlines()
    seen: set[str] = set()
    out = []
    for line in lines:
        key = _line_key(line)
        if key and key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    text = "\n".join(out)
    return text if text.endswith("\n") else text + "\n"


def apply_setup(values: dict[str, str], actor: str = "setup-wizard") -> tuple[Optional[Path], dict[str, Any]]:
    """values: a subset of FIELDS keys -> new value. Blank/missing optional
    fields are simply not written (never forced to an empty line). Returns
    (backup_path_or_None, fresh check_status())."""
    updates = {k: v.strip() for k, v in values.items() if k in FIELDS and v is not None and v.strip()}
    if not updates:
        return None, check_status()
    current = envfile.read_content()
    new_content = set_env_vars(current, updates)
    backup = envfile.write_content(new_content, actor=actor)
    return backup, check_status()

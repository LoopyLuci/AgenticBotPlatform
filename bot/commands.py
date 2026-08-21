"""Platform-agnostic slash-command core — shared by Telegram (bot/handlers.py),
Discord, and Slack (bot/platforms/*.py) so all three chat platforms offer the
same in-chat settings/management commands instead of Telegram being the only
one with anything beyond a plain message relay.

Each cmd_* function takes a CmdContext plus any args and returns the reply
text — no platform-specific objects (Update, discord.Message, Slack `say`)
ever appear here. Telegram keeps its own nicer inline-keyboard confirm flow
for desktop actions in bot/handlers.py; Discord/Slack (and Telegram, as a
fallback) get a plain-text "reply with `... confirm`" flow via cmd_desktop,
since inline buttons aren't portable across all three platforms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from bot import db, desktop, setup_wizard
from bot.backends.base import BackendError
from bot.config import config
from bot.models import KNOWN_MODELS
from bot.router import VALID_BACKENDS, router


@dataclass
class CmdContext:
    instance_id: Optional[int]
    instance_name: str
    user_id: Any
    chat_id: Any
    actor: str
    session: dict = field(default_factory=dict)  # per-chat scratch state: project_cwd, action_type


HELP_TEXT = (
    "Bot Control is online.\n\n"
    "/ask <text> — send a prompt (append --backend=api|cli|ui|hermes_cli|hermes_gateway to override)\n"
    "/status — health snapshot\n"
    "/backend show|set — router config\n"
    "/model — interactive model picker (Telegram) | /model show|set <backend> <model> — text form\n"
    "/mcp list|enable|disable|logs — MCP servers\n"
    "/start_desktop /stop_desktop /restart_desktop\n"
    "/project open <path> — set working dir for the next /ask\n"
    "/new_session — open a fresh linked chat in Claude Desktop/Hermes for this bot (ui/hermes_gateway only)"
)


async def cmd_help(ctx: CmdContext, args: list[str]) -> str:
    return HELP_TEXT


async def cmd_status(ctx: CmdContext, args: list[str]) -> str:
    ov = db.get_overview()
    d = desktop.status()
    cfg = config.current
    readiness = setup_wizard.backend_readiness()
    backend_lines = []
    for name in ("api", "cli", "ui", "hermes_cli", "hermes_gateway"):
        info = readiness[name]
        mark = "ready" if info["ready"] else f"not set up ({info['reason']})"
        used = " · in use" if info["in_use"] else ""
        backend_lines.append(f"  {name}: {mark}{used}")
    lines = [
        f"Desktop: {'running' if d.get('running') else 'stopped'}" + (f" (pid {d['pid']})" if d.get("pid") else ""),
        f"Default backend: {cfg.get('default_backend')}",
        "Backends:",
        *backend_lines,
        f"Jobs running: {ov['jobs_running']} · queued: {ov['jobs_queued']}",
        f"Completed today: {ov['completed_today']} · failed: {ov['failed_today']}",
        f"Success rate (7d): {ov['success_rate_7d']}%",
        f"Avg duration: {ov['avg_duration_ms']}ms",
        f"Config version: v{config.version}",
    ]
    return "\n".join(lines)


async def cmd_backend(ctx: CmdContext, args: list[str]) -> str:
    cfg = config.current
    if not args or args[0] == "show":
        overrides = cfg.get("action_overrides", {})
        lines = [f"default: {cfg.get('default_backend')}"]
        for action, entry in overrides.items():
            lines.append(f"{action}: {entry.get('backend')} (backup: {entry.get('backup', [])})")
        return "\n".join(lines)
    if args[0] == "set" and len(args) >= 3:
        action_or_default, name = args[1], args[2]
        if name not in VALID_BACKENDS:
            return f"unknown backend {name!r}"
        if action_or_default == "default":
            config.set_value(["default_backend"], name, actor=ctx.actor)
        else:
            config.set_value(["action_overrides", action_or_default, "backend"], name, actor=ctx.actor)
        return f"Set {action_or_default} -> {name} (v{config.version})"
    return "Usage: /backend show | /backend set <action|default> <api|cli|ui|hermes_cli|hermes_gateway>"


MODEL_BACKENDS = ("api", "hermes_cli", "hermes_gateway")


async def cmd_model(ctx: CmdContext, args: list[str]) -> str:
    cfg = config.current
    if not args or args[0] == "show":
        lines = []
        for name in MODEL_BACKENDS:
            model = (cfg.get("backends", {}).get(name) or {}).get("model")
            lines.append(f"{name}: {model or '(default)'}")
        return "\n".join(lines)
    if args[0] == "set" and len(args) >= 3:
        name, model = args[1], " ".join(args[2:]).strip()
        if name not in MODEL_BACKENDS:
            return f"unknown backend {name!r} — expected one of {MODEL_BACKENDS}"
        if not model:
            return "model name can't be empty"
        known = KNOWN_MODELS.get(name)
        if known and model not in known:
            return f"unknown model {model!r} for {name} — expected one of {known}"
        config.set_value(["backends", name, "model"], model, actor=ctx.actor)
        return f"Set {name} model -> {model} (v{config.version})"
    return "Usage: /model show | /model set <api|hermes_cli|hermes_gateway> <model>"


# --------------------------------------------- interactive picker (Telegram)
# Mirrors the real Hermes Agent Telegram bot's /model UX (confirmed against
# NousResearch/hermes-agent's docs, not guessed): a two-level inline-keyboard
# drill-down — provider list with model counts and a checkmark on the
# current one, then a paginated model list with Prev/Next/Back/Cancel,
# editing the same message in place at every step. Adapted here since we
# don't have Hermes's "providers with many models" shape: our "provider"
# level is a backend (api/hermes_cli/hermes_gateway), and only `api` has an
# enumerable model list — the two hermes backends are free text (no way to
# discover what a given Hermes install actually offers), so their picker
# page says so instead of pretending to list something we can't see.
# The keyboard widgets themselves are Telegram-specific (InlineKeyboardButton
# isn't a concept the other platforms have here) and stay in bot/handlers.py
# — this module only computes the data those widgets render.

MODEL_PAGE_SIZE = 6


def model_backend_summary() -> list[dict]:
    cfg = config.current
    default_backend = cfg.get("default_backend")
    out = []
    for name in MODEL_BACKENDS:
        known = KNOWN_MODELS.get(name)
        out.append({
            "name": name,
            "count": len(known) if known else None,
            "is_default_backend": name == default_backend,
            "current_model": (cfg.get("backends", {}).get(name) or {}).get("model"),
        })
    return out


def model_page(backend: str, page: int) -> dict:
    known = KNOWN_MODELS.get(backend) or []
    current_model = (config.current.get("backends", {}).get(backend) or {}).get("model")
    total_pages = max(1, -(-len(known) // MODEL_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * MODEL_PAGE_SIZE
    return {
        "backend": backend,
        "page": page,
        "total_pages": total_pages,
        "models": known[start : start + MODEL_PAGE_SIZE],
        "current_model": current_model,
        "has_known_list": bool(known),
    }


def apply_model(backend: str, model: str, actor: str) -> str:
    config.set_value(["backends", backend, "model"], model, actor=actor)
    return f"Model set: {backend} -> {model} (v{config.version})"


async def cmd_mcp(ctx: CmdContext, args: list[str]) -> str:
    if not args or args[0] == "list":
        servers = desktop.list_mcp_servers()
        if not servers:
            return "No MCP servers configured."
        return "\n".join(f"{'✓' if s['enabled'] else '✗'} {s['name']}" for s in servers)
    if args[0] in ("enable", "disable") and len(args) >= 2:
        name = args[1]
        try:
            if args[0] == "enable":
                desktop.enable_mcp(name)
            else:
                desktop.disable_mcp(name)
            return f"{name} {args[0]}d. Restart Desktop to apply."
        except KeyError as exc:
            return str(exc)
    if args[0] == "logs" and len(args) >= 2:
        return "\n".join(desktop.tail_mcp_log(args[1], lines=30))
    return "Usage: /mcp list | enable <name> | disable <name> | logs <name>"


async def cmd_project(ctx: CmdContext, args: list[str]) -> str:
    if len(args) >= 2 and args[0] == "open":
        path = " ".join(args[1:])
        ctx.session["project_cwd"] = path
        ctx.session["action_type"] = "project_task"
        return f"Project set to {path} — next /ask runs with action_type=project_task"
    return "Usage: /project open <path>"


_DESKTOP_ACTIONS: dict[str, Callable[[], bool]] = {
    "start": desktop.start,
    "stop": desktop.stop,
    "restart": desktop.restart,
}


async def cmd_desktop(ctx: CmdContext, action: str, args: list[str]) -> str:
    """Shared handler for /start_desktop, /stop_desktop, /restart_desktop.
    start isn't destructive; stop/restart require a trailing "confirm" arg
    when confirm_destructive is on — a plain-text equivalent of Telegram's
    inline-keyboard confirm (bot/handlers.py keeps that nicer flow for
    itself, calling straight into desktop.stop()/restart() once confirmed;
    this text-based path is what Discord/Slack use, and what Telegram falls
    back to if it ever needs to)."""
    if action == "start":
        try:
            desktop.start()
            return "Claude Desktop starting."
        except FileNotFoundError as exc:
            return str(exc)

    confirm_needed = (config.current.get("security") or {}).get("confirm_destructive", True)
    confirmed = bool(args) and args[0] == "confirm"
    if confirm_needed and not confirmed:
        return f"{action.capitalize()} Claude Desktop? Reply `/{action}_desktop confirm` to proceed."
    _DESKTOP_ACTIONS[action]()
    return f"Claude Desktop {action}ed."


async def cmd_ask(ctx: CmdContext, raw: str) -> str:
    prompt, backend_override = _parse_backend_flag(raw)
    if not prompt:
        return "Usage: /ask <text> [--backend=api|cli|ui|hermes_cli|hermes_gateway]"

    action_type = ctx.session.get("action_type", "quick_question")
    cwd = ctx.session.get("project_cwd")
    try:
        result = await router.ask(
            prompt,
            action_type=action_type,
            user_id=ctx.user_id,
            backend_override=backend_override,
            context={"cwd": cwd} if cwd else None,
            instance_id=ctx.instance_id,
            chat_id=ctx.chat_id,
        )
        return result.text
    except BackendError as exc:
        return f"Backend failed: {exc}"


async def cmd_new_session(ctx: CmdContext, args: list[str]) -> str:
    """Opens a brand-new linked chat/session in the real Claude Desktop or
    Hermes app for this bot instance — see Router.create_session(). Every
    /ask for this bot from now on reselects that exact chat instead of
    whatever happens to be open, so this is the only supported way to
    deliberately start a fresh conversation with a ui/hermes_gateway bot."""
    if ctx.instance_id is None:
        return "/new_session needs a bot instance — this chat isn't linked to one."
    try:
        key = await router.create_session(ctx.instance_id)
    except BackendError as exc:
        return f"Could not create a session: {exc}"
    return f"New session linked: {key!r}. Future messages to this bot go there."


def _parse_backend_flag(text: str) -> tuple[str, Optional[str]]:
    parts = text.rsplit("--backend=", 1)
    if len(parts) == 2:
        rest, override = parts
        override = override.strip().split()[0]
        if override in VALID_BACKENDS:
            return rest.strip(), override
    return text, None


COMMANDS: dict[str, Callable[[CmdContext, list[str]], Any]] = {
    "start": cmd_help,
    "help": cmd_help,
    "status": cmd_status,
    "backend": cmd_backend,
    "model": cmd_model,
    "mcp": cmd_mcp,
    "project": cmd_project,
    "new_session": cmd_new_session,
}

_DESKTOP_COMMANDS = {
    "start_desktop": "start",
    "stop_desktop": "stop",
    "restart_desktop": "restart",
}


async def dispatch_command(text: str, ctx: CmdContext) -> Optional[str]:
    """Parses a raw message as a slash command and runs it, returning the
    reply text — or None if `text` isn't a recognized command, so the
    caller falls through to a normal router.ask() instead of the message
    silently vanishing. /ask is handled here too (not just the bare
    prefix-less relay) so all three platforms accept the same explicit
    `/ask --backend=... <text>` form Telegram already documents."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    cmd, *rest = text[1:].split(None, 1)
    args_text = rest[0] if rest else ""
    args = args_text.split() if args_text else []

    if cmd == "ask":
        return await cmd_ask(ctx, args_text)
    if cmd in _DESKTOP_COMMANDS:
        return await cmd_desktop(ctx, _DESKTOP_COMMANDS[cmd], args)
    handler = COMMANDS.get(cmd)
    if handler is None:
        return None
    return await handler(ctx, args)

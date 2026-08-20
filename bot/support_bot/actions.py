"""Intent -> real management action wiring.

No new business logic lives here — every handler is a thin call into the
same functions the dashboard API and bot/commands.py already use
(bot/desktop.py, bot/config.py, bot/bot_instances.py, bot/router.py,
bot/db.py, bot/platform_supervisor.py), so the Support Bot can never do
something the dashboard couldn't already do, and every action it takes is
already covered by those modules' existing audit logging.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from bot import bot_instances, db, desktop, platform_supervisor
from bot.config import config
from bot.models import KNOWN_MODELS
from bot.router import VALID_BACKENDS
from bot.setup_wizard import backend_readiness
from bot.support_bot import slots


class ActionError(Exception):
    """A recognized intent that couldn't be carried out — bad/missing slot,
    unknown target, etc. Always safe to show the message verbatim to the
    user, never a raw traceback."""


def _status(text: str, actor: str) -> str:
    ov = db.get_overview()
    d = desktop.status()
    cfg = config.current
    readiness = backend_readiness()
    lines = [
        f"Desktop: {'running' if d.get('running') else 'stopped'}" + (f" (pid {d['pid']})" if d.get("pid") else ""),
        f"Default backend: {cfg.get('default_backend')}",
    ]
    for name in ("api", "cli", "ui", "hermes_cli", "hermes_gateway"):
        info = readiness[name]
        mark = "ready" if info["ready"] else f"not set up ({info['reason']})"
        lines.append(f"  {name}: {mark}")
    lines += [
        f"Jobs running: {ov['jobs_running']} · queued: {ov['jobs_queued']}",
        f"Completed today: {ov['completed_today']} · failed: {ov['failed_today']}",
        f"Success rate (7d): {ov['success_rate_7d']}%",
        f"Config version: v{config.version}",
    ]
    return "\n".join(lines)


def _list_bots(text: str, actor: str) -> str:
    instances = bot_instances.list_instances()
    if not instances:
        return "No bot instances configured yet."
    live = platform_supervisor.status()
    lines = []
    for inst in instances:
        running = live.get(inst["id"], {}).get("running", False)
        state = "running" if running else ("enabled, stopped" if inst["enabled"] else "disabled")
        lines.append(f"- {inst['name']} ({inst['platform']}/{inst['backend']}) — {state}")
    return "\n".join(lines)


def _bot_create(text: str, actor: str) -> str:
    return (
        "Creating a bot needs a platform, a bot token, and an allowed-user "
        "list — those shouldn't be typed into a chat prompt, so use the "
        "Bots tab's + button to add one securely."
    )


def _bot_edit(text: str, actor: str) -> str:
    inst = slots.find_bot_name(text)
    if inst is None:
        return "Which bot? Say the bot's name, or use the Bots tab to edit one directly."
    return (
        f"Found {inst['name']!r} ({inst['platform']}/{inst['backend']}). "
        "For safety, credential and platform edits happen in the Bots tab's "
        "edit form — I can enable/disable, restart, or delete it here if you ask."
    )


def _resolve_instance_or_raise(text: str) -> dict[str, Any]:
    inst = slots.find_bot_name(text)
    if inst is None:
        raise ActionError("I couldn't tell which bot instance you mean — say its name exactly.")
    return inst


def _bot_delete(text: str, actor: str) -> str:
    inst = _resolve_instance_or_raise(text)
    bot_instances.delete_instance(inst["id"], actor=actor)
    return f"Deleted bot instance {inst['name']!r}."


def _bot_enable(text: str, actor: str) -> str:
    inst = _resolve_instance_or_raise(text)
    bot_instances.enable_instance(inst["id"], actor=actor)
    return f"Enabled {inst['name']!r}."


def _bot_disable(text: str, actor: str) -> str:
    inst = _resolve_instance_or_raise(text)
    bot_instances.disable_instance(inst["id"], actor=actor)
    return f"Disabled {inst['name']!r}."


def _bot_restart(text: str, actor: str) -> str:
    inst = _resolve_instance_or_raise(text)
    return f"Restarted {inst['name']!r}."


async def _bot_restart_async(text: str, actor: str) -> str:
    inst = _resolve_instance_or_raise(text)
    await platform_supervisor.restart_instance(inst["id"])
    return f"Restarted {inst['name']!r}."


def _backend_show(text: str, actor: str) -> str:
    cfg = config.current
    lines = [f"default: {cfg.get('default_backend')}"]
    for action, entry in (cfg.get("action_overrides") or {}).items():
        lines.append(f"{action}: {entry.get('backend')}")
    return "\n".join(lines)


def _backend_set(text: str, actor: str) -> str:
    backend = slots.find_backend(text)
    if backend is None:
        raise ActionError(f"Which backend? One of: {', '.join(VALID_BACKENDS)}.")
    config.set_value(["default_backend"], backend, actor=actor)
    return f"Default backend set to {backend} (v{config.version})."


def _model_show(text: str, actor: str) -> str:
    cfg = config.current
    lines = []
    for name in ("api", "hermes_cli", "hermes_gateway"):
        current = ((cfg.get("backends") or {}).get(name) or {}).get("model") or "(default)"
        lines.append(f"{name}: {current}")
    return "\n".join(lines)


def _model_set(text: str, actor: str) -> str:
    backend = slots.find_backend(text) or "api"
    if backend not in ("api", "hermes_cli", "hermes_gateway"):
        raise ActionError("Only api, hermes_cli, and hermes_gateway backends have a model setting.")
    model = slots.find_model(text, backend)
    if model is None:
        raise ActionError("Which model? Say the model name, e.g. \"set the api model to claude-opus-5\".")
    known = KNOWN_MODELS.get(backend)
    if known and model not in known:
        raise ActionError(f"{model!r} isn't one of {backend}'s known models: {', '.join(known)}.")
    config.set_value(["backends", backend, "model"], model, actor=actor)
    return f"{backend} model set to {model} (v{config.version})."


def _mcp_list(text: str, actor: str) -> str:
    servers = desktop.list_mcp_servers()
    if not servers:
        return "No MCP servers configured."
    return "\n".join(f"- {s['name']}: {'enabled' if s['enabled'] else 'disabled'}" for s in servers)


def _mcp_enable(text: str, actor: str) -> str:
    name = slots.find_mcp_server_name(text)
    if name is None:
        raise ActionError("Which MCP server? I couldn't match one from what you said.")
    desktop.enable_mcp(name)
    return f"Enabled MCP server {name!r}."


def _mcp_disable(text: str, actor: str) -> str:
    name = slots.find_mcp_server_name(text)
    if name is None:
        raise ActionError("Which MCP server? I couldn't match one from what you said.")
    desktop.disable_mcp(name)
    return f"Disabled MCP server {name!r}."


def _mcp_logs(text: str, actor: str) -> str:
    name = slots.find_mcp_server_name(text)
    if name is None:
        raise ActionError("Which MCP server's logs?")
    lines = desktop.tail_mcp_log(name, lines=20)
    return f"Last {len(lines)} lines for {name!r}:\n" + "\n".join(lines)


def _desktop_start(text: str, actor: str) -> str:
    try:
        desktop.start()
    except FileNotFoundError as exc:
        raise ActionError(str(exc)) from exc
    return "Claude Desktop starting."


def _desktop_stop(text: str, actor: str) -> str:
    desktop.stop()
    return "Claude Desktop stopped."


def _desktop_restart(text: str, actor: str) -> str:
    desktop.restart()
    return "Claude Desktop restarted."


def _config_reload(text: str, actor: str) -> str:
    changed, summary = config.reload(actor=actor)
    if not changed:
        return "Config reloaded — no changes found."
    return "Config reloaded:\n" + "\n".join(summary)


def _allowed_users_list(text: str, actor: str) -> str:
    users = db.list_allowed_users()
    if not users:
        return "No legacy allowed-users entries (per-instance allowlists live on each bot instance now)."
    return "\n".join(str(u["telegram_id"]) for u in users)


def _help(text: str, actor: str) -> str:
    return (
        "I can help with: status, list bots, enable/disable/restart/delete a "
        "bot, show/set the default backend, show/set a backend's model, "
        "list/enable/disable MCP servers and read their logs, "
        "start/stop/restart Claude Desktop, reload config, and list allowed "
        "users. Just ask in plain language."
    )


# Handlers that don't touch a running process are plain sync functions;
# bot_restart needs the async platform_supervisor call, so it's registered
# separately in engine.py's ASYNC_INTENT_HANDLERS.
INTENT_HANDLERS: dict[str, Callable[[str, str], str]] = {
    "status": _status,
    "list_bots": _list_bots,
    "bot_create": _bot_create,
    "bot_edit": _bot_edit,
    "bot_delete": _bot_delete,
    "bot_enable": _bot_enable,
    "bot_disable": _bot_disable,
    "backend_show": _backend_show,
    "backend_set": _backend_set,
    "model_show": _model_show,
    "model_set": _model_set,
    "mcp_list": _mcp_list,
    "mcp_enable": _mcp_enable,
    "mcp_disable": _mcp_disable,
    "mcp_logs": _mcp_logs,
    "desktop_start": _desktop_start,
    "desktop_stop": _desktop_stop,
    "desktop_restart": _desktop_restart,
    "config_reload": _config_reload,
    "allowed_users_list": _allowed_users_list,
    "help": _help,
}

ASYNC_INTENT_HANDLERS: dict[str, Callable[[str, str], Any]] = {
    "bot_restart": _bot_restart_async,
}

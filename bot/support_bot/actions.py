"""Intent -> real management action wiring.

No new business logic lives here — every handler is a thin call into the
same functions the dashboard API and bot/commands.py already use
(bot/desktop.py, bot/config.py, bot/bot_instances.py, bot/router.py,
bot/db.py, bot/platform_supervisor.py), so the Support Bot can never do
something the dashboard couldn't already do, and every action it takes is
already covered by those modules' existing audit logging.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

from bot import bot_instances, db, desktop, envfile, platform_supervisor
from bot.config import config
from bot.router import VALID_BACKENDS
from bot.setup_wizard import backend_readiness
from bot.support_bot import slots
from bot.swarm import engine as swarm_engine


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


async def _model_set(text: str, actor: str) -> str:
    from bot.models import known_models_for

    backend = slots.find_backend(text) or "api"
    if backend not in ("api", "hermes_cli", "hermes_gateway"):
        raise ActionError("Only api, hermes_cli, and hermes_gateway backends have a model setting.")
    model = await slots.find_model(text, backend)
    if model is None:
        raise ActionError(
            "Which model? Say the exact model name (quoting it works best), "
            f"or check what's actually available for {backend} first."
        )
    known = await known_models_for(backend)
    if known and model not in known:
        raise ActionError(f"{model!r} isn't one of {backend}'s currently available models: {', '.join(known)}.")
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


# ------------------------------------------------------ jobs & swarms -----
def _jobs_list(text: str, actor: str) -> str:
    status = None
    lowered = text.lower()
    for candidate in ("failed", "running", "queued", "success", "retrying"):
        if candidate in lowered:
            status = candidate
            break
    rows = db.list_jobs(limit=10, status=status)
    if not rows:
        return f"No {status + ' ' if status else ''}jobs found."
    lines = [f"#{r['id']} {r['action_type']} · {r['backend']} · {r['status']}" for r in rows]
    return "\n".join(lines)


def _job_status(text: str, actor: str) -> str:
    job_id = slots.find_number(text)
    if job_id is None:
        raise ActionError("Which job? Say its number, e.g. \"check job #42\".")
    row = db.get_job(job_id)
    if row is None:
        raise ActionError(f"No job #{job_id} found.")
    lines = [
        f"#{row['id']} {row['action_type']} · {row['backend']} · {row['status']}",
        f"prompt: {(row['prompt'] or '')[:200]}",
    ]
    if row["result"]:
        lines.append(f"result: {row['result'][:300]}")
    if row["error"]:
        lines.append(f"error: {row['error']}")
    return "\n".join(lines)


def _swarms_list(text: str, actor: str) -> str:
    rows = db.list_swarms()
    if not rows:
        return "No swarms configured yet — use the Swarms tab to create one."
    return "\n".join(f"- {r['name']} ({r['strategy']}) — {'enabled' if r['enabled'] else 'disabled'}" for r in rows)


def _swarm_run(text: str, actor: str) -> str:
    swarm = slots.find_swarm(text)
    if swarm is None:
        raise ActionError("Which swarm? Say its name, or use the Swarms tab.")
    prompt = slots.extract_swarm_prompt(text)
    if not prompt:
        raise ActionError(f"Found {swarm['name']!r} — what prompt should it run? Say \"...with prompt: <text>\".")
    try:
        run_id = swarm_engine.start_swarm_run(swarm["id"], prompt, requested_by="support_bot")
    except ValueError as exc:
        raise ActionError(str(exc)) from exc
    return f"Started a run of {swarm['name']!r} — run id {run_id}. Ask \"swarm run status {run_id}\" to check on it."


def _swarm_run_status(text: str, actor: str) -> str:
    run_id = slots.find_swarm_run_id(text)
    if run_id is None:
        raise ActionError("Which run? Paste the run id, or check the Swarms tab's run history.")
    row = db.get_swarm_run(run_id)
    if row is None:
        raise ActionError(f"No swarm run {run_id!r} found.")
    d = swarm_engine.swarm_run_to_dict(row)
    lines = [f"status: {d['status']}", f"prompt: {(d['prompt'] or '')[:200]}"]
    if d.get("result"):
        lines.append(f"result: {d['result'][:300]}")
    if d.get("error"):
        lines.append(f"error: {d['error']}")
    return "\n".join(lines)


# --------------------------------------------- diagnostics/db/backups -----
def _diagnostics(text: str, actor: str) -> str:
    latency = db.get_latency_by_backend()
    conn = db.get_conn()
    recent_errors = conn.execute(
        "SELECT component, COUNT(*) c FROM connections_log "
        "WHERE event='request_error' AND ts >= datetime('now','-15 minutes') GROUP BY component"
    ).fetchall()
    lines = ["Latency (p50/p95, last 6h):"]
    for backend, stats in latency.items():
        lines.append(f"  {backend}: {stats.get('p50_ms', 0):.0f}ms / {stats.get('p95_ms', 0):.0f}ms")
    if recent_errors:
        lines.append("Errors in the last 15 minutes:")
        for row in recent_errors:
            lines.append(f"  {row['component']}: {row['c']}")
    else:
        lines.append("No errors in the last 15 minutes.")
    return "\n".join(lines)


def _db_status(text: str, actor: str) -> str:
    size = db.get_db_size_bytes()
    counts = db.get_table_counts()
    lines = [f"DB size: {size / (1024*1024):.2f} MB"]
    lines += [f"  {table}: {n}" for table, n in counts.items()]
    return "\n".join(lines)


def _db_vacuum(text: str, actor: str) -> str:
    db.vacuum()
    return "Database vacuumed."


def _backups_list(text: str, actor: str) -> str:
    env_backups = envfile.list_backups()
    inst_backups = bot_instances.list_backups()
    if not env_backups and not inst_backups:
        return "No backups yet — one is taken automatically before every save/restore/create/update/delete."
    lines = []
    if env_backups:
        lines.append("Environment (.env) backups:")
        lines += [f"  {b['name']} ({b['mtime']})" for b in env_backups[:10]]
    if inst_backups:
        lines.append("Bot instance backups:")
        lines += [f"  {b['name']} ({b['mtime']})" for b in inst_backups[:10]]
    return "\n".join(lines)


def _backup_restore(text: str, actor: str) -> str:
    name = slots.find_backup_name(text)
    if name is None:
        raise ActionError("Which backup? Say its name — ask \"list backups\" to see the options.")
    try:
        if name.startswith("env-"):
            envfile.restore_backup(name, actor=actor)
            return f"Restored .env from {name!r}. Takes effect on next server restart."
        elif name.startswith("instances-"):
            bot_instances.restore_backup(name, actor=actor)
            return f"Restored bot instances from {name!r}."
        raise ActionError(f"{name!r} doesn't look like a known backup name.")
    except (FileNotFoundError, ValueError) as exc:
        raise ActionError(str(exc)) from exc


# --------------------------------------------------------- toggles --------
def _settings_show(text: str, actor: str) -> str:
    cfg = config.current
    features = cfg.get("features") or {}
    security = cfg.get("security") or {}
    agent_control = cfg.get("agent_control") or {}
    return "\n".join([
        f"ui automation: {'on' if features.get('ui_automation_enabled') else 'off'}",
        f"confirm destructive actions: {'on' if security.get('confirm_destructive', True) else 'off'}",
        f"verbose telemetry: {'on' if features.get('verbose_telemetry') else 'off'}",
        f"agent control mode: {agent_control.get('mode', 'trust_all')}",
    ])


def _settings_set(text: str, actor: str) -> str:
    setting = slots.find_setting(text)
    if setting is None:
        raise ActionError(
            "Which setting? One of: ui automation, confirm destructive, verbose telemetry, agent control."
        )
    path, kind = setting
    if kind == "mode":
        mode = slots.find_agent_control_mode(text)
        if mode is None:
            raise ActionError("Agent control mode should be \"trust_all\" or \"allowlist\".")
        config.set_value(path, mode, actor=actor)
        return f"Agent control mode set to {mode} (v{config.version})."
    value = slots.find_bool(text)
    if value is None:
        raise ActionError("Say on/off (or enable/disable) for that setting.")
    config.set_value(path, value, actor=actor)
    return f"{'.'.join(path)} set to {value} (v{config.version})."


# ---------------------------------------------------- mobile & sessions ---
def _devices_list(text: str, actor: str) -> str:
    rows = db.list_devices()
    if not rows:
        return "No paired devices yet."
    return "\n".join(f"- {r['label']} ({r['platform'] or 'unknown'}) — last seen {r['last_seen']}" for r in rows)


def _device_revoke(text: str, actor: str) -> str:
    device = slots.find_device(text)
    if device is None:
        raise ActionError("Which device? Say its label, or check the Mobile tab.")
    db.revoke_api_key(device["id"])
    return f"Revoked {device['label']!r} — it can no longer connect."


def _mobile_key_create(text: str, actor: str) -> str:
    label = slots.find_quoted(text) or "New device"
    key_id, plaintext = db.create_api_key(label)
    return (
        f"Created pairing key for {label!r} (id {key_id}). Key: {plaintext}\n"
        "This is shown once — use the Mobile tab's QR code if you need it again later, "
        "or hand this key to the device's manual-entry pairing screen now."
    )


def _sessions_list(text: str, actor: str) -> str:
    q = slots.find_quoted(text)
    rows = db.list_sessions(q=q, limit=10)
    if not rows:
        return "No sessions found."
    return "\n".join(f"- #{r['id']} {r['title'] or '(untitled)'} — {r['item_count']} items, last active {r['last_activity_at']}" for r in rows)


def _session_show(text: str, actor: str) -> str:
    session_id = slots.find_number(text)
    if session_id is None:
        raise ActionError("Which session? Say its number, e.g. \"show me session 5\".")
    row = db.get_session(session_id)
    if row is None:
        raise ActionError(f"No session #{session_id} found.")
    items = db.get_session_items(session_id)
    return (
        f"#{row['id']} {row['title'] or '(untitled)'}\n"
        f"messages: {len(items['messages'])} · jobs: {len(items['jobs'])}\n"
        f"last activity: {row['last_activity_at']}"
    )


# ---------------------------------------- claude/hermes connection setup --
def _find_hermes_env_path() -> Optional[Path]:
    """Best-effort — Hermes Agent is a separate external tool with its own
    config location, not something Bot Server installs or owns. Only
    checks the Windows default location Hermes itself uses; a missing
    result just means "couldn't check," not "not installed.\""""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None
    path = Path(local_appdata) / "hermes" / ".env"
    return path if path.exists() else None


def _claude_setup_check(text: str, actor: str) -> str:
    readiness = backend_readiness()["ui"]
    d = desktop.status()
    lines = [f"ui backend: {'ready' if readiness['ready'] else 'not ready — ' + readiness['reason']}"]
    lines.append(f"Claude Desktop process: {'running (pid ' + str(d['pid']) + ')' if d.get('running') else 'not running'}")
    if not readiness["ready"]:
        lines.append(
            "To fix: install Claude Desktop, sign in once, then set CLAUDE_DESKTOP_EXE in .env if "
            "auto-detection fails. See docs/connecting-claude-and-hermes.md for the full walkthrough."
        )
    return "\n".join(lines)


def _hermes_setup_check(text: str, actor: str) -> str:
    readiness = backend_readiness()
    cli_ready = readiness["hermes_cli"]
    gw_ready = readiness["hermes_gateway"]
    lines = [
        f"hermes_cli: {'ready' if cli_ready['ready'] else 'not ready — ' + cli_ready['reason']}",
        f"hermes_gateway: {'ready' if gw_ready['ready'] else 'not ready — ' + gw_ready['reason']}",
    ]
    lines.append(
        "(This only confirms the `hermes` binary is on PATH — run `hermes status` yourself for a "
        "real auth/model check.)"
    )

    hermes_env = _find_hermes_env_path()
    if hermes_env is None:
        lines.append("Couldn't locate Hermes's own .env to check for token conflicts — check manually if needed.")
        return "\n".join(lines)

    try:
        hermes_env_text = hermes_env.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "\n".join(lines)

    hermes_tokens: set[str] = set()
    for raw_line in hermes_env_text.splitlines():
        line = raw_line.strip()
        if line.startswith("#") or "TOKEN=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip().endswith("_BOT_TOKEN") and value.strip():
            hermes_tokens.add(value.strip())

    if not hermes_tokens:
        lines.append("No active platform tokens found in Hermes's own .env — no conflict risk.")
        return "\n".join(lines)

    conflicts = []
    for inst in bot_instances.list_instances():
        token = (inst.get("credentials") or {}).get("bot_token")
        if token and token in hermes_tokens:
            conflicts.append(inst["name"])

    if conflicts:
        lines.append(
            f"⚠️ Token conflict: {', '.join(conflicts)} share a platform token with Hermes's own gateway "
            f"config ({hermes_env}). If Hermes's gateway starts, it will fight Bot Server for that token "
            "(a 'Conflict: terminated by other getUpdates request' error). Comment out that platform's "
            "token in Hermes's own .env — see docs/connecting-claude-and-hermes.md for exact steps."
        )
    else:
        lines.append("No token conflicts found between Hermes's own gateway config and Bot Server's bot instances.")
    return "\n".join(lines)


def _help(text: str, actor: str) -> str:
    return (
        "I can help with: status, list bots, enable/disable/restart/delete a "
        "bot, show/set the default backend, show/set a backend's model, "
        "list/enable/disable MCP servers and read their logs, "
        "start/stop/restart Claude Desktop, reload config, list allowed "
        "users, jobs & swarms (list/run/check status), diagnostics "
        "(errors/latency), database status/vacuum, backups (list/restore), "
        "feature toggles & agent control mode, paired devices (list/revoke/"
        "create a pairing key), sessions (list/show), and checking Claude "
        "Desktop/Hermes Agent setup. Just ask in plain language."
    )


# Handlers that don't need an async call are plain sync functions;
# bot_restart (async platform_supervisor call) and model_set (async live
# model lookup, no hardcoded fallback) are registered separately below in
# ASYNC_INTENT_HANDLERS instead.
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
    "mcp_list": _mcp_list,
    "mcp_enable": _mcp_enable,
    "mcp_disable": _mcp_disable,
    "mcp_logs": _mcp_logs,
    "desktop_start": _desktop_start,
    "desktop_stop": _desktop_stop,
    "desktop_restart": _desktop_restart,
    "config_reload": _config_reload,
    "allowed_users_list": _allowed_users_list,
    "jobs_list": _jobs_list,
    "job_status": _job_status,
    "swarms_list": _swarms_list,
    "swarm_run": _swarm_run,
    "swarm_run_status": _swarm_run_status,
    "diagnostics": _diagnostics,
    "db_status": _db_status,
    "db_vacuum": _db_vacuum,
    "backups_list": _backups_list,
    "backup_restore": _backup_restore,
    "settings_show": _settings_show,
    "settings_set": _settings_set,
    "devices_list": _devices_list,
    "device_revoke": _device_revoke,
    "mobile_key_create": _mobile_key_create,
    "sessions_list": _sessions_list,
    "session_show": _session_show,
    "claude_setup_check": _claude_setup_check,
    "hermes_setup_check": _hermes_setup_check,
    "help": _help,
}

ASYNC_INTENT_HANDLERS: dict[str, Callable[[str, str], Any]] = {
    "bot_restart": _bot_restart_async,
    "model_set": _model_set,
}

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

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from bot import db, desktop, kanban, memory, outbox, scheduler, setup_wizard, skills, slash_commands
from bot.agent_runtime import approval as agent_approval
from bot.agent_runtime import engine as agent_engine
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
    # Telegram forum-topic id (see /topic) — None outside a topic, and
    # always None on Discord/Slack. Makes a topic behave as its own fully
    # independent session, queue, and pause/stop scope, not just a
    # separate conversation history bucket.
    thread_id: Optional[Any] = None
    # Platform-specific "ask a human to approve this dangerous tool call"
    # hook for the agent-loop engine (bot/agent_runtime/approval.py) — set
    # by handlers.py's _ctx_from() to a closure that sends a real Telegram
    # message with ea: buttons. None on platforms/paths with no chat to
    # notify (approval then just times out and denies — see api_backend.py).
    notify_approval: Optional[Callable[[int, str, dict], Any]] = None
    # Optional "here's what's happening right now" hook for a long-running
    # api-backend tool loop — set by handlers.py to edit one status message
    # in place (thinking… → running tool X → …) instead of the chat
    # staying silent until the whole multi-tool-call turn finishes. None
    # means no live progress (every other platform, and paths with no
    # message to edit).
    progress_notify: Optional[Callable[[str], Any]] = None


def _build_help_text() -> str:
    return "Bot Control is online.\n\n" + "\n".join(slash_commands.help_lines())


HELP_TEXT = _build_help_text()  # kept as a platform-agnostic fallback for callers with no CmdContext yet


async def cmd_help(ctx: CmdContext, args: list[str]) -> str:
    """Dynamic per-instance /help — the static HELP_TEXT above has no way
    to know which bot it's for, so it can't mention that bot's installed
    skills; this rebuilds it fresh every call instead. `args` optionally
    filters to lines containing that text, or "skills" to show only the
    skills summary — matching Hermes's own /help [skills|<filter>]."""
    if args and args[0].lower() == "skills":
        if ctx.instance_id is None:
            return "This chat isn't linked to a bot instance."
        return skills.summary(ctx.instance_id) or "No skills installed. /skills install <path>"

    lines = slash_commands.help_lines()
    if args:
        needle = " ".join(args).lower()
        lines = [ln for ln in lines if needle in ln.lower()] or [f"No commands match {needle!r}."]

    out = ["Bot Control is online.", ""]
    out.extend(lines)
    if ctx.instance_id is not None:
        skill_rows = skills.list_for_instance(ctx.instance_id)
        if skill_rows:
            out.append("")
            out.append(f"⚡ {len(skill_rows)} skill(s) installed — /skills list, or /help skills for details.")
    return "\n".join(out)


# Qualified display names for the backend-readiness list — "cli"/"ui" alone
# don't say which model family they run, unlike hermes_cli/hermes_gateway
# which already carry it.
_BACKEND_DISPLAY_NAMES = {
    "api": "claude_api",
    "cli": "claude_cli",
    "ui": "claude_ui",
    "hermes_cli": "hermes_cli",
    "hermes_gateway": "hermes_gateway",
}


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
        backend_lines.append(f"  {_BACKEND_DISPLAY_NAMES[name]}: {mark}{used}")
    lines = [
        f"Desktop: {'running' if d.get('running') else 'stopped'}" + (f" (pid {d['pid']})" if d.get("pid") else ""),
        f"Default backend: {cfg.get('default_backend')}",
        "Backends:",
        *backend_lines,
    ]
    if ctx.instance_id is not None:
        from bot import bot_instances

        instance = bot_instances.get_instance(ctx.instance_id)
        if instance is not None:
            label = await format_model_label(instance["backend"], instance.get("model"))
            lines.append(f"Model: {label}")
    lines.extend([
        f"Jobs running: {ov['jobs_running']} · queued: {ov['jobs_queued']}",
        f"Completed today: {ov['completed_today']} · failed: {ov['failed_today']}",
        f"Success rate (7d): {ov['success_rate_7d']}%",
        f"Avg duration: {ov['avg_duration_ms']}ms",
    ])
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


# ------------------------------------- per-instance model (Telegram picker)
# /model set <backend> <model> above changes a *global* per-backend-family
# default (config.backends.<name>.model), shared by every bot instance that
# doesn't override it — useful, but not what someone typing /model in a
# specific bot's chat almost always means. Telegram's interactive /model
# (no args, or the bare `/model <name>` shorthand — see handlers.py)
# instead reads/writes bot_instances.model, the same per-instance override
# field the dashboard's own per-bot Model dropdown already edits, scoped
# to THIS bot's actual backend only — not a picker across all three model
# backends regardless of what this bot is even connected to.

INSTANCE_MODEL_PAGE_SIZE = 6

_FREE_MODEL_RE = re.compile(r"[-:_]free$", re.IGNORECASE)


def is_free_model_id(model_id: str) -> bool:
    """Free-tier model ids follow a "...-free"/"...:free" suffix convention
    across every provider Hermes's cache has shown so far (OpenRouter,
    opencode-free, etc.) — there's no pricing metadata to check instead, so
    this is a naming-convention heuristic, not a guarantee."""
    return bool(_FREE_MODEL_RE.search(model_id))


def _sort_group(model_ids) -> list[str]:
    uniq = sorted(set(model_ids))
    free = [m for m in uniq if is_free_model_id(m)]
    paid = [m for m in uniq if not is_free_model_id(m)]
    return free + paid


async def instance_model_groups(backend: str) -> list[dict]:
    """Models available to `backend`, grouped by provider (Hermes's own
    cache is already keyed by provider; the Claude family has exactly one),
    each group's models sorted with free ones first. Replaces the old flat
    instance_model_options() list, which discarded the provider structure
    Hermes's cache already gives us and interleaved every provider's models
    together."""
    from bot.models import BACKEND_FAMILY, live_api_models, live_hermes_models

    family = BACKEND_FAMILY.get(backend, "claude")
    if family == "hermes":
        grouped = live_hermes_models()
        if not grouped:
            return []
        return [
            {"provider": name, "models": _sort_group(models)}
            for name, models in sorted(grouped.items(), key=lambda kv: kv[0].lower())
            if models
        ]
    live = await live_api_models()
    models = live or KNOWN_MODELS.get("api", [])
    return [{"provider": "anthropic", "models": _sort_group(models)}] if models else []


async def instance_model_page(instance_id: int, provider: Optional[int], page: int) -> Optional[dict]:
    """Two-level picker data: with multiple provider groups and no
    `provider` index chosen yet, returns a provider-list payload (mode
    "providers"); otherwise returns a paginated model-list payload (mode
    "models") scoped to that provider group. A backend with only one
    provider group (Claude, or a Hermes install whose cache only has one
    provider) skips straight to "models" — there's nothing to pick between."""
    from bot import bot_instances

    instance = bot_instances.get_instance(instance_id)
    if instance is None:
        return None
    backend = instance["backend"]
    groups = await instance_model_groups(backend)
    current_model = instance.get("model")

    if not groups:
        return {"mode": "models", "backend": backend, "provider": None, "provider_idx": None,
                "page": 0, "total_pages": 1, "models": [], "current_model": current_model,
                "has_known_list": False}

    if len(groups) > 1 and provider is None:
        providers = [
            {
                "idx": i,
                "name": g["provider"],
                "count": len(g["models"]),
                "free_count": sum(1 for m in g["models"] if is_free_model_id(m)),
                "is_current": current_model in g["models"],
            }
            for i, g in enumerate(groups)
        ]
        return {"mode": "providers", "backend": backend, "providers": providers, "current_model": current_model}

    idx = provider if (provider is not None and 0 <= provider < len(groups)) else 0
    group = groups[idx]
    models = group["models"]
    total_pages = max(1, -(-len(models) // INSTANCE_MODEL_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * INSTANCE_MODEL_PAGE_SIZE
    return {
        "mode": "models",
        "backend": backend,
        "provider": group["provider"],
        "provider_idx": idx,
        "multi_provider": len(groups) > 1,
        "page": page,
        "total_pages": total_pages,
        "models": models[start : start + INSTANCE_MODEL_PAGE_SIZE],
        "current_model": current_model,
        "has_known_list": bool(models),
    }


async def format_model_label(backend: str, model: Optional[str]) -> str:
    """"(backend default)" for no override, "provider/model" when `model`
    is found in one of this backend's live provider groups, or the bare
    model id as a last resort (a manually-typed model that isn't in the
    live list — still worth showing, just without a provider we can't
    determine)."""
    if not model:
        return "(backend default)"
    for group in await instance_model_groups(backend):
        if model in group["models"]:
            return f"{group['provider']}/{model}"
    return model


async def apply_instance_model(instance_id: int, model: str, actor: str) -> str:
    from bot import bot_instances

    instance = bot_instances.get_instance(instance_id)
    backend = instance["backend"] if instance else "api"
    bot_instances.update_instance(instance_id, model=(model or None), actor=actor)
    label = await format_model_label(backend, model)
    return f"Model set for this bot -> {label}"


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


def _ask_context(ctx: CmdContext) -> Optional[dict]:
    cwd = ctx.session.get("project_cwd")
    out: dict = {}
    if cwd:
        out["cwd"] = cwd
    if ctx.notify_approval is not None:
        out["approval_notify"] = ctx.notify_approval
    if ctx.progress_notify is not None:
        out["progress_notify"] = ctx.progress_notify
    return out or None


def _deliver_result(ctx: CmdContext):
    """Builds the on_result callback for a queued or backgrounded turn —
    the only way its eventual outcome reaches the chat, since by the time
    it actually runs the request/reply cycle that triggered it is long
    over. Pushed via bot/outbox.py so it works for any connected
    platform, not just Telegram."""

    async def _deliver(outcome: str, result) -> None:
        if ctx.instance_id is None:
            return
        if outcome == "ran":
            text = result.text
        elif outcome == "stopped":
            text = "(stopped)"
        else:
            text = f"Backend failed: {result}"
        try:
            await outbox.send_message(ctx.instance_id, ctx.chat_id, text, thread_id=ctx.thread_id)
        except RuntimeError:
            pass  # instance disconnected between queuing and delivery — nothing to do

    return _deliver


async def cmd_ask(ctx: CmdContext, raw: str) -> str:
    prompt, backend_override = _parse_backend_flag(raw)
    if not prompt:
        return "Usage: /ask <text> [--backend=api|cli|ui|hermes_cli|hermes_gateway]"

    action_type = ctx.session.get("action_type", "quick_question")
    outcome, result = await agent_engine.run_turn(
        prompt,
        action_type=action_type,
        user_id=ctx.user_id,
        instance_id=ctx.instance_id,
        chat_id=ctx.chat_id,
        thread_id=ctx.thread_id,
        backend_override=backend_override,
        context=_ask_context(ctx),
        on_result=_deliver_result(ctx),
    )
    if outcome == "queued":
        return "A message is already in progress for this chat — yours is queued and will run next."
    if outcome == "paused":
        return "This bot is paused (see /pause) — your message is queued and will run once resumed."
    if outcome == "stopped":
        return "Stopped."
    if outcome == "error":
        return f"Backend failed: {result}"
    return result.text


async def cmd_background(ctx: CmdContext, raw: str) -> str:
    """Runs a prompt in the background — replies immediately instead of
    blocking on the answer, delivering it later via outbox when it's
    ready. Same engine path as /ask; the only difference is background=True."""
    prompt, backend_override = _parse_backend_flag(raw)
    if not prompt:
        return "Usage: /background <text>"
    action_type = ctx.session.get("action_type", "quick_question")
    outcome, _ = await agent_engine.run_turn(
        prompt,
        action_type=action_type,
        user_id=ctx.user_id,
        instance_id=ctx.instance_id,
        chat_id=ctx.chat_id,
        thread_id=ctx.thread_id,
        backend_override=backend_override,
        context=_ask_context(ctx),
        background=True,
        on_result=_deliver_result(ctx),
    )
    if outcome == "queued":
        return "Something's already running for this chat — queued, will run in the background after."
    if outcome == "paused":
        return "This bot is paused — queued, will run in the background once resumed."
    return "Running in the background — I'll send the result here when it's done."


async def cmd_stop(ctx: CmdContext, args: list[str]) -> str:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    stopped = await agent_engine.stop(ctx.instance_id, ctx.chat_id, thread_id=ctx.thread_id)
    return "Stopped." if stopped else "Nothing is running for this chat."


async def cmd_queue(ctx: CmdContext, raw: str) -> str:
    """Explicitly queues a prompt for after whatever's currently running
    finishes, without interrupting it — same effect /ask has automatically
    when this chat is already busy, offered as its own command so you can
    queue several in a row on purpose."""
    return await cmd_ask(ctx, raw)


async def cmd_steer(ctx: CmdContext, raw: str) -> str:
    """Injects a message into the currently-running turn instead of
    waiting for it to finish — delivered at the next tool-call boundary
    for the api backend's own loop (see api_backend.py), or as an
    immediately-following prompt for other backends (there's no tool-call
    boundary BotServer can see inside an external program's own loop)."""
    text = raw.strip()
    if not text:
        return "Usage: /steer <text>"
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    delivered = agent_engine.push_steer(ctx.instance_id, ctx.chat_id, text, thread_id=ctx.thread_id)
    return "Steered." if delivered else "Nothing is running for this chat to steer."


async def cmd_pause(ctx: CmdContext, args: list[str]) -> str:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    if args and args[0] == "off":
        agent_engine.resume(ctx.instance_id)
        return "Resumed — queued messages (if any) will start running now."
    agent_engine.pause(ctx.instance_id)
    return "Paused — new messages for this bot will queue instead of running. `/pause off` to resume."


async def cmd_agents(ctx: CmdContext, args: list[str]) -> str:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    running = agent_engine.list_running(ctx.instance_id)
    if not running:
        return "Nothing running for this bot right now."
    lines = ["Running:"]
    for r in running:
        where = f"chat {r['chat_id']}" + (f" topic {r['thread_id']}" if r["thread_id"] is not None else "")
        lines.append(f"  {where}" + (" (background)" if r["background"] else ""))
    return "\n".join(lines)


async def cmd_approve(ctx: CmdContext, args: list[str]) -> str:
    """Approves the oldest pending tool-call approval for this chat —
    `/approve` alone means "once", `/approve session` or `/approve always`
    extend it the same way the ea:session / ea:always buttons do."""
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    pending = agent_approval.oldest_pending(ctx.instance_id, ctx.chat_id)
    if pending is None:
        return "Nothing is waiting for approval in this chat."
    outcome = args[0] if args and args[0] in ("session", "always") else "once"
    ok = agent_approval.resolve(pending["id"], outcome, actor=str(ctx.user_id))
    return f"Approved ({outcome})." if ok else "That approval was already resolved."


async def cmd_deny(ctx: CmdContext, args: list[str]) -> str:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    pending = agent_approval.oldest_pending(ctx.instance_id, ctx.chat_id)
    if pending is None:
        return "Nothing is waiting for approval in this chat."
    ok = agent_approval.resolve(pending["id"], "deny", actor=str(ctx.user_id))
    return "Denied." if ok else "That approval was already resolved."


# ------------------------------------------------------------ checkpoints -
# Real git snapshots of the api-backend workspace (see
# bot/agent_runtime/checkpoints.py and api_backend.py's auto-checkpoint
# after each dangerous tool call) — /rollback, /undo, /branch, /compress,
# /worktree. All five refuse to run while a turn is actively running for
# this chat, since resetting the working directory out from under a
# tool-call loop mid-flight would be actively dangerous, not just confusing.

def _checkpoint_workspace(ctx: CmdContext):
    from bot.agent_runtime import tools as agent_tools

    if ctx.instance_id is None:
        return None
    return agent_tools.resolve_workspace(ctx.instance_id, ctx.session.get("project_cwd"))


def _checkpoint_busy_guard(ctx: CmdContext) -> Optional[str]:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    if agent_engine.is_running(ctx.instance_id, ctx.chat_id, ctx.thread_id):
        return "A turn is running for this chat — /stop it first before touching checkpoints."
    return None


async def cmd_rollback(ctx: CmdContext, args: list[str]) -> str:
    from bot.agent_runtime import checkpoints

    guard = _checkpoint_busy_guard(ctx)
    if guard:
        return guard
    steps = int(args[0]) if args and args[0].isdigit() else 1
    workspace = _checkpoint_workspace(ctx)
    try:
        return checkpoints.rollback(workspace, steps=steps)
    except checkpoints.CheckpointError as exc:
        return f"Rollback failed: {exc}"


async def cmd_undo(ctx: CmdContext, args: list[str]) -> str:
    from bot.agent_runtime import checkpoints

    guard = _checkpoint_busy_guard(ctx)
    if guard:
        return guard
    workspace = _checkpoint_workspace(ctx)
    try:
        return checkpoints.undo(workspace)
    except checkpoints.CheckpointError as exc:
        return f"Undo failed: {exc}"


async def cmd_branch(ctx: CmdContext, args: list[str]) -> str:
    from bot.agent_runtime import checkpoints

    guard = _checkpoint_busy_guard(ctx)
    if guard:
        return guard
    if not args:
        return "Usage: /branch <name>"
    workspace = _checkpoint_workspace(ctx)
    try:
        return checkpoints.branch(workspace, args[0])
    except checkpoints.CheckpointError as exc:
        return f"Branch failed: {exc}"


async def cmd_compress(ctx: CmdContext, args: list[str]) -> str:
    from bot.agent_runtime import checkpoints

    guard = _checkpoint_busy_guard(ctx)
    if guard:
        return guard
    workspace = _checkpoint_workspace(ctx)
    try:
        return checkpoints.compress(workspace)
    except checkpoints.CheckpointError as exc:
        return f"Compress failed: {exc}"


async def cmd_worktree(ctx: CmdContext, args: list[str]) -> str:
    from bot.agent_runtime import checkpoints

    guard = _checkpoint_busy_guard(ctx)
    if guard:
        return guard
    if not args:
        return "Usage: /worktree <name>"
    workspace = _checkpoint_workspace(ctx)
    try:
        return checkpoints.worktree(workspace, args[0])
    except checkpoints.CheckpointError as exc:
        return f"Worktree failed: {exc}"


async def cmd_new_session(ctx: CmdContext, args: list[str]) -> str:
    """Opens a brand-new linked chat/session in the real Claude Desktop or
    Hermes app, for THIS chat specifically — see Router.create_session()
    and db.link_chat_session(). Every /ask from this chat from now on
    reselects that exact conversation instead of whatever this instance's
    other chats (or the dashboard) happen to have linked, so this is the
    only supported way to deliberately start a fresh conversation with a
    ui/hermes_gateway bot. Matches Hermes's busy_policy=interrupt_then_dispatch
    for /new: if this chat has a job in flight, it asks for confirmation
    first rather than silently abandoning it."""
    if ctx.instance_id is None:
        return "/new needs a bot instance — this chat isn't linked to one."
    if agent_engine.is_running(ctx.instance_id, ctx.chat_id, ctx.thread_id) and not (args and args[0] == "confirm"):
        return "A message is still in flight for this chat. Reply `/new confirm` to start a new session anyway."
    try:
        key = await router.create_session(ctx.instance_id, chat_id=ctx.chat_id, thread_id=ctx.thread_id)
    except BackendError as exc:
        return f"Could not create a session: {exc}"
    return f"New session linked: {key!r}. Future messages from this chat go there."


async def cmd_sessions(ctx: CmdContext, args: list[str]) -> str:
    """Lists this chat's linked backend sessions — the currently-active one
    plus history, newest first. See db.list_chat_sessions()."""
    if ctx.instance_id is None:
        return "/sessions needs a bot instance — this chat isn't linked to one."
    rows = db.list_chat_sessions(ctx.instance_id, chat_id=ctx.chat_id, thread_id=ctx.thread_id, limit=20)
    if not rows:
        return "No linked sessions yet for this chat — use /new to create one."
    lines = ["Sessions for this chat (newest first):"]
    for r in rows:
        mark = "* " if r["archived_at"] is None else "  "
        label = r["title"] or r["desktop_session_key"]
        lines.append(f"{mark}#{r['id']} {label} — linked {r['created_at']}")
    lines.append("\nUse /resume <id> to switch back to an earlier one.")
    return "\n".join(lines)


async def cmd_resume(ctx: CmdContext, args: list[str]) -> str:
    """Re-links this chat to a previously-created backend session instead
    of starting a new one — see Router.resume_session()."""
    if ctx.instance_id is None:
        return "/resume needs a bot instance — this chat isn't linked to one."
    if not args or not args[0].lstrip("#").isdigit():
        return "Usage: /resume <id> — see /sessions for the list of ids."
    chat_session_id = int(args[0].lstrip("#"))
    try:
        target = await router.resume_session(ctx.instance_id, ctx.chat_id, chat_session_id, thread_id=ctx.thread_id)
    except BackendError as exc:
        return f"Could not resume: {exc}"
    label = target["title"] or target["desktop_session_key"]
    return f"Resumed session #{chat_session_id} ({label}). Future messages from this chat go there."


async def cmd_title(ctx: CmdContext, args: list[str]) -> str:
    """Sets a display title on this chat's currently-active linked
    session, shown by /sessions instead of the raw backend key."""
    if ctx.instance_id is None:
        return "/title needs a bot instance — this chat isn't linked to one."
    active = db.get_active_chat_session(ctx.instance_id, ctx.chat_id, thread_id=ctx.thread_id)
    if active is None:
        return "This chat has no active session to title — use /new first."
    title = " ".join(args).strip()
    if not title:
        return "Usage: /title <name>"
    db.set_chat_session_title(active["id"], title)
    return f"Session #{active['id']} titled {title!r}."


async def cmd_topic(ctx: CmdContext, args: list[str]) -> str:
    """Telegram forum-topic session isolation — each topic in a group with
    Topics enabled gets its own independent /new, /queue, /pause, /stop,
    everything, the same as a completely separate chat would. See
    bot/db.py's chat_sessions.thread_id and bot/agent_runtime/engine.py's
    3-part (instance_id, chat_id, thread_id) key."""
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."

    if args and args[0].lower() == "list":
        rows = db.list_topic_sessions(ctx.instance_id, ctx.chat_id)
        if not rows:
            return "No topics have their own session yet in this group."
        lines = ["Topics with their own session:"]
        for r in rows:
            label = r["title"] or r["desktop_session_key"]
            lines.append(f"  topic {r['thread_id']}: {label}")
        return "\n".join(lines)

    if ctx.thread_id is None:
        return (
            "This is the group's main thread, not a forum topic. Enable Topics in this "
            "group's settings, then open (or create) a topic and run /new there — each "
            "topic gets its own independent session. /topic list shows what's active."
        )

    active = db.get_active_chat_session(ctx.instance_id, ctx.chat_id, thread_id=ctx.thread_id)
    if active is None:
        return "This topic has no session yet — /new creates one, scoped to just this topic."
    label = active["title"] or active["desktop_session_key"]
    return f"This topic's active session: #{active['id']} {label}. /new, /resume, /sessions all apply to just this topic."


async def cmd_commands(ctx: CmdContext, args: list[str]) -> str:
    page = int(args[0]) if args and args[0].isdigit() else 1
    return slash_commands.commands_page(page)


async def cmd_profile(ctx: CmdContext, args: list[str]) -> str:
    """Shows this chat's active bot instance name and backend — the closest
    equivalent to Hermes's /profile (active profile name + home directory),
    adapted since BotServer's unit is a bot instance, not a CLI profile."""
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    from bot import bot_instances

    instance = bot_instances.get_instance(ctx.instance_id)
    if instance is None:
        return f"Bot instance {ctx.instance_id} no longer exists."
    lines = [
        f"Bot: {instance['name']} (#{instance['id']})",
        f"Platform: {instance['platform']}",
        f"Backend: {instance['backend']}",
        f"Persona: {instance['persona']}",
    ]
    if instance.get("model"):
        lines.append(f"Model: {instance['model']}")
    return "\n".join(lines)


# ------------------------------------------------------------------- cron -

async def cmd_cron(ctx: CmdContext, raw: str) -> str:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    parts = raw.split(None, 1)
    sub = parts[0].lower() if parts else "list"
    rest = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        rows = scheduler.list_for_chat(ctx.instance_id, ctx.chat_id, thread_id=ctx.thread_id)
        if not rows:
            return "No scheduled commands for this chat. /cron add <interval> <prompt>"
        lines = ["Scheduled:"]
        for r in rows:
            mark = "on " if r["enabled"] else "off"
            lines.append(f"  #{r['id']} [{mark}] {r['kind']} every {r['interval_s']}s: {r['prompt'][:60]!r}")
        return "\n".join(lines)
    if sub == "add":
        bits = rest.split(None, 1)
        if len(bits) < 2:
            return "Usage: /cron add <interval> <prompt>"
        try:
            interval_s = scheduler.parse_duration(bits[0])
            sched_id = scheduler.create(ctx.instance_id, ctx.chat_id, "cron", bits[1], interval_s, thread_id=ctx.thread_id)
        except scheduler.ScheduleError as exc:
            return str(exc)
        return f"Scheduled #{sched_id}, every {bits[0]}."
    if sub in ("remove", "pause", "resume") and rest.strip().isdigit():
        sched_id = int(rest.strip())
        {"remove": scheduler.remove, "pause": scheduler.pause, "resume": scheduler.resume}[sub](sched_id)
        return f"{sub.capitalize()}d #{sched_id}."
    return "Usage: /cron list | add <interval> <prompt> | pause <id> | resume <id> | remove <id>"


async def cmd_loop(ctx: CmdContext, raw: str) -> str:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    max_runs: Optional[int] = None
    text = raw
    if "--times" in text:
        text, _, times_s = text.partition("--times")
        try:
            max_runs = int(times_s.strip().split()[0])
        except (ValueError, IndexError):
            return "Usage: /loop <interval> <prompt> [--times N]"
    bits = text.strip().split(None, 1)
    if len(bits) < 2:
        return "Usage: /loop <interval> <prompt> [--times N]"
    try:
        interval_s = scheduler.parse_duration(bits[0])
        sched_id = scheduler.create(
            ctx.instance_id, ctx.chat_id, "loop", bits[1], interval_s, max_runs=max_runs, thread_id=ctx.thread_id
        )
    except scheduler.ScheduleError as exc:
        return str(exc)
    suffix = f", {max_runs} times" if max_runs else ""
    return f"Looping #{sched_id}, every {bits[0]}{suffix}. /cron list to manage it."


async def cmd_heartbeat(ctx: CmdContext, raw: str) -> str:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    existing = [
        r for r in scheduler.list_for_chat(ctx.instance_id, ctx.chat_id, thread_id=ctx.thread_id)
        if r["kind"] == "heartbeat"
    ]
    args = raw.strip().split()
    sub = args[0].lower() if args else ""

    if sub == "status" or not sub:
        if not existing:
            return "No heartbeat set. /heartbeat every <interval> <prompt>"
        r = existing[0]
        return f"Heartbeat #{r['id']}: every {r['interval_s']}s, {'on' if r['enabled'] else 'paused'}: {r['prompt'][:80]!r}"
    if sub == "pause" and existing:
        scheduler.pause(existing[0]["id"])
        return "Heartbeat paused."
    if sub == "resume" and existing:
        scheduler.resume(existing[0]["id"])
        return "Heartbeat resumed."
    if sub == "clear" and existing:
        scheduler.remove(existing[0]["id"])
        return "Heartbeat cleared."
    if sub == "every":
        bits = raw.strip().split(None, 2)
        if len(bits) < 3:
            return "Usage: /heartbeat every <interval> <prompt>"
        try:
            interval_s = scheduler.parse_duration(bits[1])
        except scheduler.ScheduleError as exc:
            return str(exc)
        for r in existing:
            scheduler.remove(r["id"])  # one heartbeat per chat
        sched_id = scheduler.create(ctx.instance_id, ctx.chat_id, "heartbeat", bits[2], interval_s, thread_id=ctx.thread_id)
        return f"Heartbeat #{sched_id} set, every {bits[1]}."
    return "Usage: /heartbeat every <interval> <prompt> | status | pause | resume | clear"


# ----------------------------------------------------------------- kanban -

async def cmd_kanban(ctx: CmdContext, args: list[str]) -> str:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    sub = args[0].lower() if args else "boards"

    if sub == "boards":
        boards = kanban.list_boards(ctx.instance_id)
        if not boards:
            return "No boards yet. /kanban add <board> <column> <text> creates one."
        return "Boards: " + ", ".join(b["name"] for b in boards)
    if sub == "list":
        board = args[1] if len(args) > 1 else "default"
        return kanban.format_board(ctx.instance_id, board)
    if sub == "add" and len(args) >= 4:
        board, column, text = args[1], args[2], " ".join(args[3:])
        try:
            card = kanban.add_card(ctx.instance_id, board, column, text)
        except kanban.KanbanError as exc:
            return str(exc)
        return f"Added #{card['id']} to {board}/{column}."
    if sub == "move" and len(args) >= 3 and args[1].isdigit():
        try:
            card = kanban.move_card(ctx.instance_id, int(args[1]), args[2])
        except kanban.KanbanError as exc:
            return str(exc)
        return f"Card #{card['id']} moved to {card['column_name']}."
    if sub == "done" and len(args) >= 2 and args[1].isdigit():
        try:
            card = kanban.move_card(ctx.instance_id, int(args[1]), "done")
        except kanban.KanbanError as exc:
            return str(exc)
        return f"Card #{card['id']} marked done."
    if sub == "delete" and len(args) >= 2 and args[1].isdigit():
        ok = kanban.delete_card(ctx.instance_id, int(args[1]))
        return "Deleted." if ok else "Card not found."
    return "Usage: /kanban boards | list <board> | add <board> <column> <text> | move <id> <column> | done <id> | delete <id>"


# ----------------------------------------------------------------- memory -

async def cmd_memory(ctx: CmdContext, raw: str) -> str:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    args = raw.strip().split()
    sub = args[0].lower() if args else "pending"

    if sub in ("pending", ""):
        rows = memory.pending(ctx.instance_id)
        if not rows:
            return "Nothing pending."
        return "Pending:\n" + "\n".join(f"  #{r['id']} {r['content'][:100]!r}" for r in rows)
    if sub == "approve" and len(args) >= 2 and args[1].isdigit():
        entry = memory.approve(int(args[1]))
        return f"Approved #{args[1]}." if entry else "Not found or already resolved."
    if sub == "reject" and len(args) >= 2 and args[1].isdigit():
        entry = memory.reject(int(args[1]))
        return f"Rejected #{args[1]}." if entry else "Not found or already resolved."
    if sub == "approval" and len(args) >= 2 and args[1] in ("on", "off"):
        memory.set_approval_required(ctx.instance_id, args[1] == "on", actor=str(ctx.user_id))
        return f"Memory approval gate: {args[1]}."
    if sub == "add":
        content = raw.strip()[3:].strip() if raw.strip().lower().startswith("add") else ""
        if not content:
            return "Usage: /memory add <text>"
        entry_id, approved = memory.remember(ctx.instance_id, content, source="user")
        return f"Saved as #{entry_id}" + ("." if approved else ", pending approval.")
    return "Usage: /memory [pending] | approve <id> | reject <id> | approval on|off | add <text>"


# ----------------------------------------------------------------- skills -

async def cmd_skills(ctx: CmdContext, args: list[str]) -> str:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    sub = args[0].lower() if args else "list"

    if sub == "list":
        rows = skills.list_for_instance(ctx.instance_id)
        if not rows:
            return "No skills installed. /skills install <path>"
        return "Skills:\n" + "\n".join(f"  {r['name']}: {r['description']}" for r in rows)
    if sub == "install" and len(args) >= 2:
        try:
            info = skills.install(ctx.instance_id, " ".join(args[1:]))
        except skills.SkillError as exc:
            return str(exc)
        return f"Installed {info['name']!r}: {info['description']}"
    if sub == "remove" and len(args) >= 2:
        ok = skills.remove(ctx.instance_id, args[1])
        return "Removed." if ok else "Not found."
    if sub == "inspect" and len(args) >= 2:
        content = skills.get_content(ctx.instance_id, args[1])
        return content if content is not None else "Not found."
    return "Usage: /skills list | install <path> | remove <name> | inspect <name>"


# ------------------------------------------------------------------ usage -

async def cmd_usage(ctx: CmdContext, args: list[str]) -> str:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    u = db.get_usage_summary(ctx.instance_id)
    return (
        f"Tokens today: {u['tokens_today']} ({u['jobs_today']} jobs)\n"
        f"Tokens total: {u['tokens_total']} ({u['jobs_total']} jobs)"
    )


async def cmd_insights(ctx: CmdContext, args: list[str]) -> str:
    if ctx.instance_id is None:
        return "This chat isn't linked to a bot instance."
    days = 7
    if args and args[0].isdigit():
        days = max(1, min(90, int(args[0])))
    data = db.get_insights(ctx.instance_id, days=days)
    if not data["by_day"]:
        return f"No activity in the last {days} day(s)."
    lines = [f"Last {days} day(s) — {data['messages_in']} messages in:"]
    for day, stats in sorted(data["by_day"].items()):
        lines.append(f"  {day}: {stats['success']} ok, {stats['failed']} failed, {stats['tokens']} tokens")
    return "\n".join(lines)


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
    "new": cmd_new_session,
    "sessions": cmd_sessions,
    "resume": cmd_resume,
    "title": cmd_title,
    "profile": cmd_profile,
    "stop": cmd_stop,
    "pause": cmd_pause,
    "agents": cmd_agents,
    "approve": cmd_approve,
    "deny": cmd_deny,
    "rollback": cmd_rollback,
    "undo": cmd_undo,
    "branch": cmd_branch,
    "compress": cmd_compress,
    "worktree": cmd_worktree,
    "kanban": cmd_kanban,
    "topic": cmd_topic,
    "skills": cmd_skills,
    "usage": cmd_usage,
    "insights": cmd_insights,
    "commands": cmd_commands,
}

# Commands whose handler wants the raw remainder text, not a split arg
# list — same reasoning as /ask always having (prompt can contain
# arbitrary punctuation/backend flags that word-splitting would mangle).
_RAW_ARG_COMMANDS: dict[str, Callable[[CmdContext, str], Any]] = {
    "ask": cmd_ask,
    "background": cmd_background,
    "queue": cmd_queue,
    "steer": cmd_steer,
    "cron": cmd_cron,
    "loop": cmd_loop,
    "heartbeat": cmd_heartbeat,
    "memory": cmd_memory,
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
    silently vanishing. /ask (and the other raw-text commands) is handled
    here too, not just the bare prefix-less relay, so all three platforms
    accept the same explicit `/ask --backend=... <text>` form Telegram
    already documents."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    cmd, *rest = text[1:].split(None, 1)
    cmd = slash_commands.resolve_command(cmd) or cmd
    args_text = rest[0] if rest else ""
    args = args_text.split() if args_text else []

    if cmd in _RAW_ARG_COMMANDS:
        return await _RAW_ARG_COMMANDS[cmd](ctx, args_text)
    if cmd in _DESKTOP_COMMANDS:
        return await cmd_desktop(ctx, _DESKTOP_COMMANDS[cmd], args)
    handler = COMMANDS.get(cmd)
    if handler is None:
        return None
    return await handler(ctx, args)

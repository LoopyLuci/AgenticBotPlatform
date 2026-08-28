"""Single source of truth for every slash command's metadata — name,
aliases, description, category — shared by the Telegram native "/" command
menu, /help, and command dispatch (bot/main.py, bot/handlers.py,
bot/commands.py).

Modeled on the real Hermes Agent's `hermes_cli/commands.py` COMMAND_REGISTRY
/ telegram_menu_commands() (confirmed by reading that source directly, not
guessed): one registry, alias resolution through it, and a priority list
that pins the commands most worth surfacing to the front of Telegram's
"/" menu when there isn't room for everything.

Command *behavior* lives elsewhere (bot/commands.py for the
platform-agnostic logic, bot/handlers.py for Telegram-specific rich
handlers like inline-keyboard confirms) — this module only answers "what
commands exist, what do they mean, and in what order should Telegram show
them."
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CommandDef:
    name: str
    description: str
    category: str = "General"
    aliases: tuple[str, ...] = ()
    args_hint: str = ""
    raw_args: bool = False  # True: handler gets the raw remainder text, not a split arg list
    admin_only: bool = False  # reserved for the permission-tier system (bot/slash_access.py)
    busy_policy: str = "dispatch"  # reserved for the agent-loop engine: dispatch|reject|interrupt_then_dispatch


COMMAND_DEFS: tuple[CommandDef, ...] = (
    CommandDef("start", "Show available commands", category="Info"),
    CommandDef("help", "Show available commands", category="Info"),
    CommandDef("ask", "Send a prompt to the bot", category="Session",
               args_hint="<text> [--backend=api|cli|ui|hermes_cli|hermes_gateway]", raw_args=True),
    CommandDef("status", "Show a health/status snapshot", category="Info"),
    CommandDef("gateway", "Show backend readiness for this bot's model family", category="Config"),
    CommandDef("backend", "Show or change the router backend", category="Config",
               args_hint="show | set <action|default> <backend>"),
    CommandDef("model", "Show or change the model (interactive picker)", category="Config",
               args_hint="[show | set <backend> <model>]"),
    CommandDef("mcp", "Manage MCP servers", category="Tools", args_hint="list | enable | disable | logs <name>"),
    CommandDef("project", "Set the working directory for the next /ask", category="Session", args_hint="open <path>"),
    CommandDef("new", "Start a new session (fresh history)", category="Session",
               aliases=("new_session",), busy_policy="interrupt_then_dispatch"),
    CommandDef("sessions", "List this chat's linked sessions", category="Session"),
    CommandDef("resume", "Resume a previous session", category="Session", args_hint="<id>"),
    CommandDef("title", "Title this chat's active session", category="Session", args_hint="<name>"),
    CommandDef("profile", "Show this chat's active bot/backend", category="Info"),
    CommandDef("whoami", "Show your slash-command access tier", category="Info"),
    CommandDef("commands", "Browse all commands, paginated", category="Info", args_hint="[page]"),
    CommandDef("stop", "Stop whatever's running for this chat", category="Session", busy_policy="interrupt_then_dispatch"),
    CommandDef("background", "Run a prompt in the background", category="Session", args_hint="<text>", raw_args=True),
    CommandDef("queue", "Queue a prompt for after the current one", category="Session", args_hint="<text>", raw_args=True),
    CommandDef("steer", "Inject a message into the running turn", category="Session", args_hint="<text>", raw_args=True),
    CommandDef("pause", "Pause new turns for this bot (queues instead)", category="Session", args_hint="[off]"),
    CommandDef("agents", "Show what's currently running for this bot", category="Session"),
    CommandDef("approve", "Approve the oldest pending tool call", category="Session", args_hint="[session|always]"),
    CommandDef("deny", "Deny the oldest pending tool call", category="Session"),
    CommandDef("rollback", "Reset the workspace back N checkpoints (git)", category="Checkpoints", args_hint="[n]"),
    CommandDef("undo", "Reset the workspace back one checkpoint", category="Checkpoints"),
    CommandDef("branch", "Create and switch to a new workspace branch", category="Checkpoints", args_hint="<name>"),
    CommandDef("compress", "Squash this session's checkpoints into one commit", category="Checkpoints"),
    CommandDef("worktree", "Create a linked worktree for parallel work", category="Checkpoints", args_hint="<name>"),
    CommandDef("cron", "Manage scheduled prompts", category="Tools",
               args_hint="list | add <interval> <prompt> | pause <id> | resume <id> | remove <id>", raw_args=True),
    CommandDef("loop", "Re-run a prompt on a recurring interval", category="Tools",
               args_hint="<interval> <prompt> [--times N]", raw_args=True),
    CommandDef("heartbeat", "A recurring prompt that fires when idle", category="Tools",
               args_hint="every <interval> <prompt> | status | pause | resume | clear", aliases=("hb",), raw_args=True),
    CommandDef("topic", "Show/list this forum topic's independent session", category="Session", args_hint="[list]"),
    CommandDef("kanban", "A per-bot kanban board", category="Tools",
               args_hint="boards | list <board> | add <board> <col> <text> | move <id> <col> | done <id> | delete <id>"),
    CommandDef("memory", "Review or add long-term memory", category="Tools",
               args_hint="[pending] | approve <id> | reject <id> | approval on|off | add <text>", raw_args=True),
    CommandDef("skills", "Manage locally-installed skills", category="Tools",
               args_hint="list | install <path> | remove <name> | inspect <name>"),
    CommandDef("usage", "Show this bot's token usage", category="Info"),
    CommandDef("insights", "Show recent activity by day", category="Info", args_hint="[days]"),
    CommandDef("start_desktop", "Start Claude Desktop", category="Desktop"),
    CommandDef("stop_desktop", "Stop Claude Desktop", category="Desktop"),
    CommandDef("restart_desktop", "Restart Claude Desktop", category="Desktop"),
)

COMMAND_REGISTRY: dict[str, CommandDef] = {c.name: c for c in COMMAND_DEFS}

ALIASES: dict[str, str] = {
    alias: c.name for c in COMMAND_DEFS for alias in c.aliases
}

# Pins the commands worth seeing first in Telegram's "/" menu when there
# isn't room for everything (mirrors the intent of Hermes's
# _TELEGRAM_MENU_PRIORITY, sized down to what BotServer actually has).
_TELEGRAM_MENU_PRIORITY: tuple[str, ...] = (
    "help", "ask", "stop", "status", "gateway", "new", "sessions", "resume", "model", "backend", "mcp", "project",
    "background", "queue", "steer", "pause", "agents", "approve", "deny",
    "rollback", "undo", "branch", "compress", "worktree",
    "cron", "loop", "heartbeat", "kanban", "memory", "skills", "usage", "insights", "topic", "commands",
    "title", "profile", "whoami", "start_desktop", "stop_desktop", "restart_desktop", "start",
)

_MENU_DESCRIPTION_MAX = 60


def resolve_command(raw: str) -> str | None:
    """Canonical name for a typed command word (case-insensitive), or None
    if it isn't a known command or alias."""
    raw = (raw or "").strip().lower()
    if raw in COMMAND_REGISTRY:
        return raw
    return ALIASES.get(raw)


def get(name: str) -> CommandDef | None:
    canonical = resolve_command(name)
    return COMMAND_REGISTRY.get(canonical) if canonical else None


def all_dispatchable_names() -> list[str]:
    """Every canonical command name plus every alias — the full list of
    strings Telegram's CommandHandler should match, so `/new` and
    `/new_session` both reach the same command."""
    names = list(COMMAND_REGISTRY.keys())
    names.extend(ALIASES.keys())
    return names


def telegram_menu_commands(max_commands: int = 60) -> list[tuple[str, str]]:
    """(name, description) pairs for Telegram's native "/" command menu,
    priority commands first (in the pinned order), then everything else
    alphabetically, capped at max_commands. Aliases never appear — one
    entry per canonical command, matching Hermes's behavior."""
    max_commands = max(1, min(max_commands, 100))  # Telegram's own setMyCommands cap
    ordered: list[str] = [n for n in _TELEGRAM_MENU_PRIORITY if n in COMMAND_REGISTRY]
    rest = sorted(n for n in COMMAND_REGISTRY if n not in ordered)
    ordered.extend(rest)
    ordered = ordered[:max_commands]
    out = []
    for name in ordered:
        desc = COMMAND_REGISTRY[name].description
        if len(desc) > _MENU_DESCRIPTION_MAX:
            desc = desc[: _MENU_DESCRIPTION_MAX - 1].rstrip() + "…"
        out.append((name, desc))
    return out


COMMANDS_PAGE_SIZE = 15  # matches the real Hermes Agent's own Telegram page size


def commands_page(page: int, page_size: int = COMMANDS_PAGE_SIZE) -> str:
    """Text-paginated full command browser for /commands — alphabetical,
    same shape Hermes uses (header with page count, a nav-hint footer),
    since a flat alphabetical list scales to however many commands exist
    without needing inline-keyboard state."""
    names = sorted(COMMAND_REGISTRY.keys())
    total = len(names)
    total_pages = max(1, -(-total // page_size))
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    chunk = names[start : start + page_size]

    lines = [f"Commands ({total} total, page {page}/{total_pages})", ""]
    for name in chunk:
        c = COMMAND_REGISTRY[name]
        hint = f" {c.args_hint}" if c.args_hint else ""
        lines.append(f"/{name}{hint} — {c.description}")
    nav = []
    if page > 1:
        nav.append(f"/commands {page - 1} ← prev")
    if page < total_pages:
        nav.append(f"next → /commands {page + 1}")
    if nav:
        lines.append("")
        lines.append(" | ".join(nav))
    return "\n".join(lines)


def help_lines() -> list[str]:
    """One line per canonical command, grouped by category, for /help."""
    by_category: dict[str, list[CommandDef]] = {}
    for c in COMMAND_DEFS:
        by_category.setdefault(c.category, []).append(c)
    lines: list[str] = []
    for category in sorted(by_category):
        lines.append(f"{category}:")
        for c in by_category[category]:
            hint = f" {c.args_hint}" if c.args_hint else ""
            alias_note = f" (alias: /{c.aliases[0]})" if c.aliases else ""
            lines.append(f"  /{c.name}{hint} — {c.description}{alias_note}")
    return lines

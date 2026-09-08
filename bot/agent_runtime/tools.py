"""Real tool execution for the agent-loop engine's tool-use turns — shell
commands, file read/write, directory listing, and read-only git
inspection. Every call is sandboxed to one working directory (the
session's `cwd`, either an explicit /project path or a per-instance
default workspace created on demand) — relative paths that try to escape
it via `..` are rejected, and absolute paths outside it are rejected too.
That boundary applies uniformly whether or not the workspace itself was
explicitly chosen by the bot's operator, since "don't let the model read
or write outside the one directory it's supposed to be working in" is a
sane rule either way.

`run_shell` is intentionally unrestricted *within* that directory (same
shape as Claude Code's own Bash tool) — it's gated by the approval flow
in approval.py, not by a command allowlist here, since a fixed
allowlist would break normal agentic work (git, package managers,
compilers) for no real safety gain once a human has already approved it.
"""

from __future__ import annotations

import asyncio
import contextvars
import json as _json_module
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT


def _json_dumps(value: Any) -> str:
    return _json_module.dumps(value)

# Ambient recursion depth for delegate_to_instance — a ContextVar rather
# than a parameter threaded through execute_tool()'s signature, since a
# nested router.ask() call from inside a tool call runs as a direct
# `await` within the same task (not a separate one), so a ContextVar set
# for the duration of that nested call is naturally visible to it and
# nowhere else. Mirrors Hermes's own delegate_task using contextvars for
# the same "how deep are we already" question (see the Hermes-swarm
# plan's Phase 4 research).
_delegation_depth: contextvars.ContextVar[int] = contextvars.ContextVar("delegation_depth", default=0)

WORKSPACES_ROOT = PROJECT_ROOT / "data" / "agent_workspaces"

SHELL_TIMEOUT_S = 60
MAX_OUTPUT_CHARS = 8000
MAX_READ_CHARS = 20000

# Tools that can change state or run arbitrary code — gated behind
# approval.py. Everything else (reads) runs immediately. update_agent_config
# is included because it can change ANOTHER instance's identity/behavior/
# can_target list — a compromised or misbehaving agent using it to
# re-route its own or another instance's permissions is exactly the kind
# of consequential change write_file already requires a human for.
DANGEROUS_TOOLS = {
    "run_shell", "write_file", "update_agent_config",
    # Per the user's explicit choice: agent-authored plugin code is
    # trusted, unsandboxed, full-process-privilege Python (see
    # docs/adr/0007-plugins-are-trusted-local-code.md) — the same trust
    # boundary run_shell already accepts. Creating one writes and
    # immediately activates new code; re-enabling a previously-disabled
    # one reactivates existing code. Both go through the same human
    # approval gate as run_shell/write_file, never auto-installed.
    "create_plugin", "enable_plugin",
}

# Admin control surface (see docs/adr/0008-single-instance-admin-tool-gate.md
# and the "Admin control surface" plan) — fleet-wide administration
# (other bot instances, global backend config, agent_settings/auto_manage
# for OTHER instances, the emergency stop, lifecycle hooks). Offered only
# to the one bot instance flagged agent_settings.is_admin_instance=True
# (see native_backend.py's schema filter) and defended-in-depth by
# _require_admin() at dispatch time below. ADMIN_TOOLS_ELEVATED additionally
# requires the calling turn's context["device_tier"] be "elevated" or
# "unrestricted" (Server Chat/Support Bot only — Telegram turns never
# carry a device_tier, so these never reach a Telegram-driven turn).
ADMIN_TOOLS_STANDARD = frozenset({
    "admin_list_bot_instances", "admin_get_bot_instance", "admin_create_bot_instance",
    "admin_update_bot_instance", "admin_delete_bot_instance", "admin_set_default_backend",
    "admin_get_agent_settings", "admin_set_agent_settings",
    "admin_get_auto_manage_config", "admin_set_auto_manage_config",
    "admin_engage_estop", "admin_disengage_estop", "admin_get_estop_status",
    "admin_list_hooks", "admin_add_hook", "admin_enable_hook", "admin_disable_hook", "admin_remove_hook",
})
ADMIN_TOOLS_ELEVATED = frozenset({
    "admin_list_devices", "admin_mint_device_key", "admin_set_device_tier", "admin_revoke_device",
    "admin_db_vacuum", "admin_restore_snapshot",
    "admin_desktop_start", "admin_desktop_stop", "admin_desktop_restart",
})
ADMIN_TOOLS = ADMIN_TOOLS_STANDARD | ADMIN_TOOLS_ELEVATED

DANGEROUS_TOOLS |= {
    "admin_create_bot_instance", "admin_update_bot_instance", "admin_delete_bot_instance",
    "admin_set_default_backend", "admin_set_agent_settings", "admin_set_auto_manage_config",
    "admin_engage_estop", "admin_disengage_estop",
    "admin_add_hook", "admin_enable_hook", "admin_disable_hook", "admin_remove_hook",
    "admin_mint_device_key", "admin_set_device_tier", "admin_revoke_device",
    "admin_db_vacuum", "admin_restore_snapshot",
    "admin_desktop_start", "admin_desktop_stop", "admin_desktop_restart",
}


def _require_admin(instance_id: Optional[int]) -> None:
    from bot import agent_settings

    if instance_id is None or not agent_settings.get(instance_id)["is_admin_instance"]:
        raise ToolError("this tool is restricted to the designated admin bot instance")


def _require_elevated_device(context_device_tier: Optional[str]) -> None:
    if context_device_tier not in ("elevated", "unrestricted"):
        raise ToolError("this tool requires an 'elevated' or 'unrestricted' device permission tier")


def _redact_credentials(credentials: dict) -> dict:
    """Masks any credential-shaped value (token/password/secret/key) to
    its last 4 characters before it's ever formatted into a tool's return
    string, which can land in a Telegram/Server-Chat transcript — even
    for admin_create_bot_instance's own response to a token the operator
    just supplied, since once it's in a chat transcript that's a second
    place the secret now lives."""
    import re

    redacted = {}
    for key, value in (credentials or {}).items():
        if isinstance(value, str) and re.search(r"token|password|secret|key", key, re.IGNORECASE):
            redacted[key] = f"...{value[-4:]}" if len(value) >= 4 else "...(hidden)"
        else:
            redacted[key] = value
    return redacted

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "run_shell",
        "description": "Run a shell command in the session's working directory. Requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The shell command to run."}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file's contents, relative to the working directory.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write (overwrite) a text file, relative to the working directory. Creates parent directories as needed. Requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_dir",
        "description": "List files and subdirectories at a path relative to the working directory (default: the working directory itself).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Defaults to '.'"}},
        },
    },
    {
        "name": "git_status",
        "description": "Show `git status --short` for the working directory.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "git_diff",
        "description": "Show `git diff` (unstaged changes) for the working directory.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "save_memory",
        "description": (
            "Remember a fact for future sessions with this chat — persists across /new. "
            "May require human approval before it takes effect; the tool result says which."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string", "description": "The fact to remember, in plain English."}},
            "required": ["content"],
        },
    },
    {
        "name": "read_skill",
        "description": "Load the full content of one of your available skills by name (see the system prompt's skill list).",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "install_skill",
        "description": (
            "Register a file already in your own working directory as a new skill you can load later with "
            "read_skill — its first line becomes the one-line description shown in your own skill list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path within your working directory to the skill file."}},
            "required": ["path"],
        },
    },
    {
        "name": "list_skills",
        "description": "List every skill currently available to you (name + one-line description).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_skill",
        "description": (
            "Author a brand-new skill directly from text — no file needed first. Use this to save something "
            "you just learned, or a set of instructions the user asked you to remember for future use with "
            "read_skill. Skills are plain descriptive text, never executed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short identifier — letters, digits, _ or - only."},
                "description": {"type": "string", "description": "One-line summary shown in skill listings."},
                "content": {"type": "string", "description": "The full skill text read_skill will return."},
                "global_": {
                    "type": "boolean",
                    "description": "Make this visible to every bot instance, not just yours. Requires manager persona.",
                },
            },
            "required": ["name", "content"],
        },
    },
    {
        "name": "remove_skill",
        "description": "Delete one of your own skills by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "create_plugin",
        "description": (
            "Author a brand-new callable TOOL by writing real Python code — for capabilities plain instructions "
            "or a skill can't provide. This is trusted, unsandboxed, full-privilege code (the same trust level "
            "as run_shell) and REQUIRES human approval before it can run. The file must define a module-level "
            "setup(api) function calling api.register_tool(name, description, input_schema, handler) and/or "
            "api.register_command(...) — see an existing plugin under data/plugins/ for the exact shape."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Plugin name — lowercase letters/digits/underscore, starting with a letter."},
                "description": {"type": "string", "description": "One-line summary of what this plugin adds."},
                "code": {"type": "string", "description": "The full plugin.py source code."},
            },
            "required": ["name", "code"],
        },
    },
    {
        "name": "enable_plugin",
        "description": "Re-activate a previously-disabled plugin's tools/commands. Requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "disable_plugin",
        "description": "Deactivate a plugin's tools/commands without deleting it.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "remove_plugin",
        "description": "Permanently delete a plugin.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "list_plugins",
        "description": "List every installed plugin (name, description, enabled state, tools/commands it registers).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "kanban_add_card",
        "description": "Add a card to one of your kanban boards (created automatically the first time it's named).",
        "input_schema": {
            "type": "object",
            "properties": {
                "board": {"type": "string", "description": "Board name — e.g. 'default'."},
                "column": {"type": "string", "description": "Column name — e.g. 'todo', 'doing', 'done'."},
                "text": {"type": "string", "description": "The card's text."},
            },
            "required": ["board", "column", "text"],
        },
    },
    {
        "name": "kanban_list_cards",
        "description": "List every card on one of your kanban boards, grouped by column.",
        "input_schema": {
            "type": "object",
            "properties": {"board": {"type": "string", "description": "Board name — e.g. 'default'."}},
            "required": ["board"],
        },
    },
    {
        "name": "kanban_move_card",
        "description": "Move one of your kanban cards to a different column.",
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "integer"},
                "column": {"type": "string", "description": "The column to move it to."},
            },
            "required": ["card_id", "column"],
        },
    },
    {
        "name": "schedule_command",
        "description": (
            "Schedule a prompt to run for you repeatedly, like /cron — e.g. a recurring status check or "
            "reminder. Runs through the exact same agent loop (same tools, same approval gating) as a "
            "manually-typed message."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "A short label for what this is, e.g. 'status_check'."},
                "prompt": {"type": "string", "description": "The prompt to run on each firing."},
                "interval": {"type": "string", "description": "e.g. '30s', '10m', '2h', '1d', or a bare number of seconds."},
                "chat_id": {"type": "string", "description": "Which chat to post results into. Omit for instance-wide (no specific chat)."},
                "thread_id": {"type": "string", "description": "Optional thread/topic id within that chat."},
                "max_runs": {"type": "integer", "description": "Optional cap on how many times this fires before it stops itself."},
            },
            "required": ["kind", "prompt", "interval"],
        },
    },
    {
        "name": "list_schedules",
        "description": "List your own scheduled commands (from schedule_command or /cron).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pause_schedule",
        "description": "Pause one of your scheduled commands without deleting it.",
        "input_schema": {
            "type": "object",
            "properties": {"schedule_id": {"type": "integer"}},
            "required": ["schedule_id"],
        },
    },
    {
        "name": "remove_schedule",
        "description": "Permanently remove one of your scheduled commands.",
        "input_schema": {
            "type": "object",
            "properties": {"schedule_id": {"type": "integer"}},
            "required": ["schedule_id"],
        },
    },
    {
        "name": "delegate_to_instance",
        "description": (
            "Ask another registered bot instance (Claude- or Hermes-backed — any backend) a question and "
            "wait for its reply, the same 'manager delegates to a worker' pattern Hermes's own delegate_task "
            "gives Hermes agents. Only instances your own can_target list allows are reachable when the "
            "dashboard's agent_control mode is 'allowlist'; under the default 'trust_all' mode any instance "
            "is reachable. Bounded by agent_runtime.max_delegation_depth to prevent an accidental delegation "
            "cycle between two instances that target each other."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_instance": {"type": "string", "description": "Target bot_instances.id (or exact name)."},
                "prompt": {"type": "string", "description": "The question/task to send it."},
            },
            "required": ["target_instance", "prompt"],
        },
    },
    {
        "name": "spawn_subagent",
        "description": (
            "Spawn one or more disposable, ephemeral sub-agents to work on independent subtasks in parallel — "
            "the same 'decompose and delegate' pattern Hermes Agent's own delegate_task tool gives Hermes "
            "agents, but with a real capability Hermes's own tool doesn't have: each task can pick its OWN "
            "model and effort, not just one shared setting for the whole batch. Unlike delegate_to_instance "
            "(which addresses ONE specific, persistent registered bot instance), these workers are throwaway: "
            "no bot_instances row is created for them, and their history isn't kept beyond this call. "
            "role='leaf' (default) workers can't spawn further sub-agents, reconfigure other instances, save "
            "memory, or write shared project context; role='orchestrator' keeps those abilities, bounded by "
            "agent_runtime.max_delegation_depth — an orchestrator child makes its own spawn_subagent call for "
            "its own children, deciding their count/model/effort itself, the same way you're deciding for it.\n\n"
            "You are trusted to actively choose swarm composition, not just accept defaults: call "
            "list_available_models first if you're unsure what's available/cheapest, then pick a model and "
            "effort level per task that fits its difficulty — e.g. low effort on a fast/free model for "
            "simple parallel lookups, higher effort on a stronger model for anything requiring real "
            "reasoning. Batch-level provider/model/effort/max_children are the default for every task; any "
            "task may override provider+model and/or effort individually. Effort levels (least to most): "
            "none, minimal, low, medium, high, xhigh, max, ultra — omit for no explicit preference.\n\n"
            "By default this call blocks until every child finishes and returns {dispatch_id, children: "
            "[{index, goal, model, status: 'ok'|'error', result_excerpt}]}. Pass background=true to return "
            "immediately instead — {dispatch_id, children: [{index, goal}]} — so you can keep working in "
            "this same turn while they run; check on them with list_subagents, nudge one with "
            "steer_subagent, or cancel one with stop_subagent, using the dispatch_id this call returns. "
            "Either way, the dispatch_id stays valid afterward for list_subagents to look results up again "
            "later, including from a later message if this turn ends first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "One entry per subtask — all run in parallel, bounded by max_children.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string", "description": "The subtask's own goal/prompt."},
                            "output_schema": {
                                "type": "object",
                                "description": "Optional JSON Schema the child's final answer must match "
                                "(validated, with one bounded retry on failure).",
                            },
                            "provider": {"type": "string", "description": "Override the batch-level provider for just this task. Give together with model."},
                            "model": {"type": "string", "description": "Override the batch-level model for just this task. Give together with provider."},
                            "effort": {
                                "type": "string",
                                "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
                                "description": "Override the batch-level effort for just this task.",
                            },
                        },
                        "required": ["goal"],
                    },
                },
                "role": {"type": "string", "enum": ["leaf", "orchestrator"], "description": "Defaults to 'leaf'."},
                "provider": {"type": "string", "description": "Named provider from config/providers.yaml, used for every task that doesn't override it. Give together with model, or omit both."},
                "model": {"type": "string", "description": "Model id at that provider, used for every task that doesn't override it. Give together with provider, or omit both to inherit your own."},
                "effort": {
                    "type": "string",
                    "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
                    "description": "Effort level for every task that doesn't override it — least to most: none, minimal, low, medium, high, xhigh, max, ultra.",
                },
                "max_children": {"type": "integer", "description": "Caps parallelism for this call, further capped by native_agent.max_concurrent_children."},
                "background": {"type": "boolean", "description": "Return immediately with a dispatch_id instead of waiting for every child to finish. Defaults to false."},
            },
            "required": ["tasks"],
        },
    },
    {
        "name": "list_subagents",
        "description": (
            "Check on spawn_subagent children — omit dispatch_id to list every dispatch you currently have "
            "(useful after starting one or more background dispatches), or pass one dispatch_id for that "
            "batch's live per-child status ('running'/'ok'/'error'/'stopped') and results so far. Works for "
            "both background and already-finished blocking dispatches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dispatch_id": {"type": "string", "description": "A dispatch_id returned by an earlier spawn_subagent call. Omit to list all of yours."},
            },
        },
    },
    {
        "name": "steer_subagent",
        "description": (
            "Send a mid-turn nudge to one still-running spawn_subagent child — the same idea as the /steer "
            "command a human can send you, given to a child you spawned. Delivered before that child's next "
            "tool call; has no effect on a child that has already finished (use list_subagents to check first "
            "if unsure)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dispatch_id": {"type": "string", "description": "The dispatch this child belongs to."},
                "child_index": {"type": "integer", "description": "Which child within that dispatch (its 'index' from spawn_subagent/list_subagents)."},
                "message": {"type": "string", "description": "The nudge to send."},
            },
            "required": ["dispatch_id", "child_index", "message"],
        },
    },
    {
        "name": "stop_subagent",
        "description": (
            "Cancel one still-running spawn_subagent child before it finishes on its own — its ephemeral "
            "session is marked 'stopped', distinct from a natural 'error'. Has no effect on a child that has "
            "already finished."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dispatch_id": {"type": "string", "description": "The dispatch this child belongs to."},
                "child_index": {"type": "integer", "description": "Which child within that dispatch (its 'index' from spawn_subagent/list_subagents)."},
            },
            "required": ["dispatch_id", "child_index"],
        },
    },
    {
        "name": "consult_models",
        "description": (
            "Get a second (or third, etc.) opinion by asking multiple models the same question in "
            "parallel — mixture-of-agents consensus, the same capability Hermes Agent's own MoA mode "
            "gives it, as an on-demand tool instead of an always-on mode. Pass 2+ references to ask; "
            "omit aggregator to see every reference's raw labeled answer yourself, or give one to have "
            "it synthesize them into a single final answer. Omit a reference's provider for the built-in "
            "Claude API; otherwise name a provider from config/providers.yaml. One reference failing "
            "doesn't fail the whole call — its own labeled block just notes the failure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "references": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "provider": {"type": "string", "description": "Omit for the built-in Claude API."},
                            "model": {"type": "string"},
                        },
                        "required": ["model"],
                    },
                },
                "aggregator": {
                    "type": "object",
                    "description": "Optional — if given, one more call synthesizes every reference's answer into one.",
                    "properties": {
                        "provider": {"type": "string"},
                        "model": {"type": "string"},
                    },
                    "required": ["model"],
                },
            },
            "required": ["question", "references"],
        },
    },
    {
        "name": "dispatch_batch_completions",
        "description": (
            "Submit many independent, plain (no tool use) completions to Anthropic's real Message "
            "Batches API for later, bulk retrieval at a real ~50% cost discount versus a live call — "
            "processed asynchronously over minutes to hours, not immediately. Real API constraint: a "
            "batched completion is one single-shot call, so it can NOT use tools the way spawn_subagent's "
            "children can — use this only for plain question-answering/summarization/classification work "
            "at scale, not for anything needing file/shell access. Returns a batch_id immediately; poll "
            "it with check_batch_status, then read results with get_batch_results once it says \"ended\"."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "custom_id": {"type": "string", "description": "Your own unique id for this task, used to match its result later."},
                            "goal": {"type": "string"},
                        },
                        "required": ["custom_id", "goal"],
                    },
                },
                "model": {"type": "string", "description": "The Anthropic model every task in this batch runs on."},
                "system_prompt": {"type": "string", "description": "Optional — applied to every task in the batch."},
            },
            "required": ["tasks", "model"],
        },
    },
    {
        "name": "check_batch_status",
        "description": "Check a Message Batches dispatch's progress (see dispatch_batch_completions). processing_status is \"ended\" once every task has finished (succeeded, errored, canceled, or expired).",
        "input_schema": {
            "type": "object",
            "properties": {"batch_id": {"type": "string"}},
            "required": ["batch_id"],
        },
    },
    {
        "name": "get_batch_results",
        "description": "Fetch a Message Batches dispatch's real results (see dispatch_batch_completions) — only call once check_batch_status reports processing_status \"ended\".",
        "input_schema": {
            "type": "object",
            "properties": {"batch_id": {"type": "string"}},
            "required": ["batch_id"],
        },
    },
    {
        "name": "get_my_profile",
        "description": (
            "Your own identity as a small markdown document: name, backend, persona, model override, "
            "which other instances you can target, and your own custom instructions. Call this whenever "
            "you need to state your own bot_instances name (e.g. as source_instance for delegate_to_instance "
            "or the botserver MCP tools) rather than guessing it."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_agent_config",
        "description": (
            "Update another bot instance's name, custom instructions, persona, model, or can_target list "
            "on the fly — the lever a manager needs to (re)configure the workers it runs, without going "
            "through the dashboard. Only instances your can_target list allows are editable when agent_control "
            "is in 'allowlist' mode (same gate delegate_to_instance uses); under 'trust_all' any instance is "
            "editable. Only the fields you pass are changed — omit anything you don't want to touch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_instance": {"type": "string", "description": "Target bot_instances.id (or exact name)."},
                "name": {"type": "string", "description": "New display name."},
                "custom_instructions": {"type": "string", "description": "Replaces the target's entire custom_instructions text."},
                "persona": {"type": "string", "description": "One of bot/personas.py's presets."},
                "model": {"type": "string", "description": "Per-instance model override (backend-specific format)."},
                "can_target": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "Replaces the target's own can_target list (which OTHER instances it, in turn, may reach).",
                },
            },
            "required": ["target_instance"],
        },
    },
    {
        "name": "admin_list_bot_instances",
        "description": "List every bot instance (id, name, platform, backend, enabled) — credentials redacted. Admin-only.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "admin_get_bot_instance",
        "description": "Full config for one bot instance by id or exact name — credentials redacted. Admin-only.",
        "input_schema": {
            "type": "object",
            "properties": {"target_instance": {"type": "string", "description": "bot_instances.id or exact name."}},
            "required": ["target_instance"],
        },
    },
    {
        "name": "admin_create_bot_instance",
        "description": "Create a new bot instance. Admin-only, requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "platform": {"type": "string"}, "backend": {"type": "string"},
                "credentials": {"type": "object", "description": "e.g. {\"bot_token\": \"...\"} for Telegram."},
                "allowed_user_ids": {"type": "array", "items": {}, "description": "Platform user ids permitted to use this bot."},
                "model": {"type": "string"}, "persona": {"type": "string"}, "enabled": {"type": "boolean"},
            },
            "required": ["name", "platform", "backend", "credentials", "allowed_user_ids"],
        },
    },
    {
        "name": "admin_update_bot_instance",
        "description": "Update any bot instance's config (name/platform/backend/credentials/allowed_user_ids/admin_user_ids/action_overrides/can_target/enabled/model/custom_instructions/persona/hermes_home/desktop_*). Admin-only, requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {"target_instance": {"type": "string"}, "fields": {"type": "object", "description": "Only the fields to change."}},
            "required": ["target_instance", "fields"],
        },
    },
    {
        "name": "admin_delete_bot_instance",
        "description": "Permanently delete a bot instance. Cannot target the calling admin instance itself. Requires confirm=true. Admin-only, requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {"target_instance": {"type": "string"}, "confirm": {"type": "boolean"}},
            "required": ["target_instance", "confirm"],
        },
    },
    {
        "name": "admin_set_default_backend",
        "description": "Change the global default backend for new action types (Claude and Hermes each keep their own default slot). Admin-only, requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {"backend": {"type": "string"}},
            "required": ["backend"],
        },
    },
    {
        "name": "admin_get_agent_settings",
        "description": "Get another instance's agent_settings (max_concurrent_children, worker/fallback provider+model, efforts, require_plan_approval). is_admin_instance is never shown here. Admin-only.",
        "input_schema": {
            "type": "object",
            "properties": {"target_instance": {"type": "string", "description": "Omit for the process-wide default row."}},
        },
    },
    {
        "name": "admin_set_agent_settings",
        "description": "Set another instance's agent_settings. is_admin_instance can NEVER be changed through this tool — dashboard/MCP only. Admin-only, requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_instance": {"type": "string", "description": "Omit for the process-wide default row."},
                "fields": {"type": "object", "description": "Any agent_settings field except is_admin_instance."},
            },
            "required": ["fields"],
        },
    },
    {
        "name": "admin_get_auto_manage_config",
        "description": "Get another instance's auto-manage config (enabled, trigger, interval, chat_id, goal_template). Admin-only.",
        "input_schema": {
            "type": "object",
            "properties": {"target_instance": {"type": "string"}},
            "required": ["target_instance"],
        },
    },
    {
        "name": "admin_set_auto_manage_config",
        "description": "Set another instance's auto-manage config. Admin-only, requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {"target_instance": {"type": "string"}, "fields": {"type": "object"}},
            "required": ["target_instance", "fields"],
        },
    },
    {
        "name": "admin_engage_estop",
        "description": "Engage BotServer's global emergency stop — no new work starts anywhere until disengaged. Admin-only, requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
        },
    },
    {
        "name": "admin_disengage_estop",
        "description": "Disengage BotServer's global emergency stop. Admin-only, requires human approval.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "admin_get_estop_status",
        "description": "Read BotServer's global emergency-stop status. Admin-only.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "admin_list_hooks",
        "description": "List every configured lifecycle hook (PreToolUse/PostToolUse/SessionStart/UserPromptSubmit). Admin-only.",
        "input_schema": {
            "type": "object",
            "properties": {"event": {"type": "string"}},
        },
    },
    {
        "name": "admin_add_hook",
        "description": "Add a lifecycle hook that runs a local shell command on the given event (trusted local code, same trust boundary as run_shell). Admin-only, requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event": {"type": "string", "description": "PreToolUse, PostToolUse, SessionStart, or UserPromptSubmit."},
                "command": {"type": "string"}, "matcher": {"type": "string"}, "target_instance": {"type": "string", "description": "Omit for a global hook."},
            },
            "required": ["event", "command"],
        },
    },
    {
        "name": "admin_enable_hook",
        "description": "Enable a previously-disabled hook by id. Admin-only, requires human approval.",
        "input_schema": {"type": "object", "properties": {"hook_id": {"type": "integer"}}, "required": ["hook_id"]},
    },
    {
        "name": "admin_disable_hook",
        "description": "Disable a hook by id without deleting it. Admin-only, requires human approval.",
        "input_schema": {"type": "object", "properties": {"hook_id": {"type": "integer"}}, "required": ["hook_id"]},
    },
    {
        "name": "admin_remove_hook",
        "description": "Permanently remove a hook by id. Admin-only, requires human approval.",
        "input_schema": {"type": "object", "properties": {"hook_id": {"type": "integer"}}, "required": ["hook_id"]},
    },
    {
        "name": "admin_list_devices",
        "description": "List every paired device (id, label, permission_tier, online status) — Android/Server-Chat exclusive, requires an elevated-or-higher calling device tier. Admin-only.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "admin_mint_device_key",
        "description": "Mint a pairing key for a new device at a given permission tier (capped at the calling device's own tier). Android/Server-Chat exclusive. Admin-only, requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {"label": {"type": "string"}, "tier": {"type": "string", "description": "none, standard, elevated, or unrestricted."}},
            "required": ["label"],
        },
    },
    {
        "name": "admin_set_device_tier",
        "description": "Change a paired device's permission tier — only a strictly lower-tier device may be targeted, never a peer/superior/self. Android/Server-Chat exclusive. Admin-only, requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {"key_id": {"type": "integer"}, "tier": {"type": "string"}},
            "required": ["key_id", "tier"],
        },
    },
    {
        "name": "admin_revoke_device",
        "description": "Revoke a paired device's key — only a strictly lower-tier device may be targeted. Android/Server-Chat exclusive. Admin-only, requires human approval.",
        "input_schema": {"type": "object", "properties": {"key_id": {"type": "integer"}}, "required": ["key_id"]},
    },
    {
        "name": "admin_db_vacuum",
        "description": "Vacuum the SQLite database. Destructive local operation, Android/Server-Chat exclusive. Admin-only, requires human approval.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "admin_restore_snapshot",
        "description": "Restore BotServer's config+DB to a previously-created snapshot. Destructive local operation, Android/Server-Chat exclusive. Admin-only, requires human approval.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "admin_desktop_start",
        "description": "Start the local Claude Desktop app. Destructive local operation, Android/Server-Chat exclusive. Admin-only, requires human approval.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "admin_desktop_stop",
        "description": "Stop the local Claude Desktop app. Destructive local operation, Android/Server-Chat exclusive. Admin-only, requires human approval.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "admin_desktop_restart",
        "description": "Restart the local Claude Desktop app. Destructive local operation, Android/Server-Chat exclusive. Admin-only, requires human approval.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_project_context",
        "description": (
            "Read a shared, cross-instance markdown document — the way a swarm of workers and their manager "
            "keep something like project status or shared notes in sync, independent of any one instance's "
            "own conversation history. Every registered instance can read every doc. Call list_project_context "
            "first if you don't know what docs exist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Doc name, e.g. 'status'."}},
            "required": ["name"],
        },
    },
    {
        "name": "write_project_context",
        "description": (
            "Create or replace a shared, cross-instance markdown document (see read_project_context) — "
            "e.g. post a project-status update every worker and the manager can read. Keep it concise "
            "(a few KB max) — this is meant to be read on every turn cheaply, not as a general file store."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Doc name, e.g. 'status'."},
                "content": {"type": "string", "description": "Full markdown content — replaces whatever was there."},
            },
            "required": ["name", "content"],
        },
    },
    {
        "name": "list_project_context",
        "description": "List every shared context doc's name, size, and when/who last updated it.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


TOOL_SCHEMA_NAMES = frozenset(t["name"] for t in TOOL_SCHEMAS)


def all_tool_schemas() -> list[dict[str, Any]]:
    """Built-in tool schemas plus every tool a loaded plugin has
    registered (bot/plugins.py) plus every tool a currently-connected
    external MCP server exposes (bot/agent_runtime/mcp_client.py) — what
    a backend should actually offer the model this turn."""
    from bot import plugins as plugin_registry
    from bot.agent_runtime import mcp_client

    return TOOL_SCHEMAS + plugin_registry.tool_schemas() + mcp_client.external_tool_schemas()


def is_dangerous(name: str) -> bool:
    from bot import plugins as plugin_registry

    return name in DANGEROUS_TOOLS or plugin_registry.is_dangerous_tool(name)


class ToolError(Exception):
    pass


def resolve_workspace(instance_id: int, cwd_override: Optional[str]) -> Path:
    if cwd_override:
        path = Path(cwd_override).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    path = (WORKSPACES_ROOT / str(instance_id)).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_path(workspace: Path, rel_path: str) -> Path:
    candidate = (workspace / rel_path).resolve() if not Path(rel_path).is_absolute() else Path(rel_path).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        raise ToolError(f"path {rel_path!r} is outside the working directory ({workspace})")
    return candidate


async def _run_subprocess(args: list[str], cwd: Path) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            " ".join(args) if len(args) > 1 else args[0],
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        raise ToolError(str(exc)) from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"timed out after {SHELL_TIMEOUT_S}s")
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    text = out.decode(errors="replace")
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n… truncated ({len(text)} chars total)"
    return f"{text}\n[exit code {proc.returncode}]"


async def execute_tool(
    name: str, tool_input: dict, *, workspace: Path, instance_id: Optional[int] = None,
    device_tier: Optional[str] = None,
) -> str:
    if name == "run_shell":
        command = tool_input.get("command") or ""
        if not command.strip():
            raise ToolError("command can't be empty")
        return await _run_subprocess([command], workspace)

    if name == "read_file":
        path = _safe_path(workspace, tool_input.get("path") or "")
        if not path.exists():
            raise ToolError(f"{tool_input.get('path')!r} does not exist")
        if not path.is_file():
            raise ToolError(f"{tool_input.get('path')!r} is not a file")
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS] + f"\n… truncated ({len(text)} chars total)"
        return text

    if name == "write_file":
        path = _safe_path(workspace, tool_input.get("path") or "")
        content = tool_input.get("content", "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {tool_input.get('path')}"

    if name == "list_dir":
        path = _safe_path(workspace, tool_input.get("path") or ".")
        if not path.exists():
            raise ToolError(f"{tool_input.get('path', '.')!r} does not exist")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return "\n".join(entries) if entries else "(empty)"

    if name == "git_status":
        return await _run_subprocess(["git", "status", "--short"], workspace)

    if name == "git_diff":
        return await _run_subprocess(["git", "diff"], workspace)

    if name == "save_memory":
        from bot import memory as bot_memory

        content = (tool_input.get("content") or "").strip()
        if not content:
            raise ToolError("content can't be empty")
        if instance_id is None:
            raise ToolError("save_memory needs an instance context")
        entry_id, approved = bot_memory.remember(instance_id, content, source="tool")
        return (
            f"Saved as memory #{entry_id} (approved, active now)."
            if approved
            else f"Saved as memory #{entry_id}, pending human approval (/memory approve {entry_id})."
        )

    if name == "read_skill":
        from bot import skills as bot_skills

        skill_name = (tool_input.get("name") or "").strip()
        if instance_id is None:
            raise ToolError("read_skill needs an instance context")
        content = bot_skills.get_content(instance_id, skill_name)
        if content is None:
            raise ToolError(f"no skill named {skill_name!r} — see the system prompt's skill list")
        return content

    if name == "install_skill":
        from bot import skills as bot_skills

        if instance_id is None:
            raise ToolError("install_skill needs an instance context")
        rel_path = tool_input.get("path") or ""
        # Same workspace sandbox read_file/write_file enforce — without
        # this, install_skill would be a path outside the intended
        # boundary: read any file on disk into a "skill" here, then
        # read_skill it back out, regardless of the workspace sandbox
        # every other file-reading tool respects.
        safe_path = _safe_path(workspace, rel_path)
        try:
            return _json_dumps(bot_skills.install(instance_id, str(safe_path)))
        except bot_skills.SkillError as exc:
            raise ToolError(str(exc))

    if name == "list_skills":
        from bot import skills as bot_skills

        if instance_id is None:
            raise ToolError("list_skills needs an instance context")
        return _json_dumps(bot_skills.list_for_instance(instance_id))

    if name == "create_skill":
        from bot import bot_instances, skills as bot_skills

        if instance_id is None:
            raise ToolError("create_skill needs an instance context")
        global_ = bool(tool_input.get("global_"))
        if global_:
            # A global skill is visible to every instance at once — there's
            # no single "target" for agent_control.can_target's per-instance
            # allowlist to check, so this reuses the same manager-like trust
            # convention (personas.MANAGER_LIKE_PERSONAS) rather than
            # inventing a new wildcard allowlist concept.
            from bot import personas

            caller = bot_instances.get_instance(instance_id)
            if not caller or not personas.is_manager_like(caller.get("persona")):
                raise ToolError("creating a global skill requires a manager-persona instance")
        try:
            result = bot_skills.create(
                instance_id,
                tool_input.get("name") or "",
                tool_input.get("description") or "",
                tool_input.get("content") or "",
                global_=global_,
            )
        except bot_skills.SkillError as exc:
            raise ToolError(str(exc))
        return _json_dumps(result)

    if name == "remove_skill":
        from bot import skills as bot_skills

        if instance_id is None:
            raise ToolError("remove_skill needs an instance context")
        skill_name = (tool_input.get("name") or "").strip()
        removed = bot_skills.remove(instance_id, skill_name)
        return f"Removed skill {skill_name!r}." if removed else f"No skill named {skill_name!r} found."

    if name == "create_plugin":
        from bot.envfile import PROJECT_ROOT
        from bot import plugins as plugin_registry

        plugin_name = (tool_input.get("name") or "").strip()
        code = tool_input.get("code") or ""
        if not plugin_name or not code.strip():
            raise ToolError("name and code are required")
        # plugins.install() derives its registered name from the FILE's
        # own stem (Path(path).stem), not its parent directory — the file
        # must be named <name>.py, not a fixed "plugin.py", or every
        # agent-authored plugin would register as the same literal name
        # "plugin" regardless of what was actually requested.
        plugin_dir = PROJECT_ROOT / "data" / "plugins" / plugin_name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        plugin_path = plugin_dir / f"{plugin_name}.py"
        plugin_path.write_text(code, encoding="utf-8")
        try:
            info = plugin_registry.install(str(plugin_path))
        except plugin_registry.PluginError as exc:
            raise ToolError(str(exc))
        return _json_dumps(info)

    if name == "enable_plugin":
        from bot import plugins as plugin_registry

        plugin_name = (tool_input.get("name") or "").strip()
        try:
            return _json_dumps(plugin_registry.enable(plugin_name))
        except plugin_registry.PluginError as exc:
            raise ToolError(str(exc))

    if name == "disable_plugin":
        from bot import plugins as plugin_registry

        plugin_name = (tool_input.get("name") or "").strip()
        try:
            return _json_dumps(plugin_registry.disable(plugin_name))
        except plugin_registry.PluginError as exc:
            raise ToolError(str(exc))

    if name == "remove_plugin":
        from bot import plugins as plugin_registry

        plugin_name = (tool_input.get("name") or "").strip()
        removed = plugin_registry.remove(plugin_name)
        return f"Removed plugin {plugin_name!r}." if removed else f"No plugin named {plugin_name!r} found."

    if name == "list_plugins":
        from bot import plugins as plugin_registry

        return _json_dumps(plugin_registry.list_plugins())

    if name == "kanban_add_card":
        from bot import kanban

        if instance_id is None:
            raise ToolError("kanban_add_card needs an instance context")
        board = tool_input.get("board") or "default"
        column = tool_input.get("column") or "todo"
        text = tool_input.get("text") or ""
        try:
            return _json_dumps(kanban.add_card(instance_id, board, column, text))
        except kanban.KanbanError as exc:
            raise ToolError(str(exc))

    if name == "kanban_list_cards":
        from bot import kanban

        if instance_id is None:
            raise ToolError("kanban_list_cards needs an instance context")
        board = tool_input.get("board") or "default"
        return _json_dumps(kanban.list_cards(instance_id, board))

    if name == "kanban_move_card":
        from bot import kanban

        if instance_id is None:
            raise ToolError("kanban_move_card needs an instance context")
        card_id = tool_input.get("card_id")
        column = tool_input.get("column") or "todo"
        if card_id is None:
            raise ToolError("card_id is required")
        try:
            return _json_dumps(kanban.move_card(instance_id, int(card_id), column))
        except kanban.KanbanError as exc:
            raise ToolError(str(exc))

    if name == "schedule_command":
        from bot import scheduler

        if instance_id is None:
            raise ToolError("schedule_command needs an instance context")
        kind = tool_input.get("kind") or ""
        prompt = tool_input.get("prompt") or ""
        interval = tool_input.get("interval") or ""
        chat_id = tool_input.get("chat_id")
        thread_id = tool_input.get("thread_id")
        max_runs = tool_input.get("max_runs")
        if not prompt.strip():
            raise ToolError("prompt is required")
        try:
            interval_s = scheduler.parse_duration(interval)
            sched_id = scheduler.create(
                instance_id, chat_id, kind, prompt, interval_s, max_runs=max_runs, thread_id=thread_id,
            )
        except scheduler.ScheduleError as exc:
            raise ToolError(str(exc))
        return _json_dumps({"id": sched_id})

    if name == "list_schedules":
        from bot import scheduler

        if instance_id is None:
            raise ToolError("list_schedules needs an instance context")
        return _json_dumps(scheduler.list_for_chat(instance_id, chat_id=None))

    if name in ("pause_schedule", "remove_schedule"):
        from bot import db, scheduler

        if instance_id is None:
            raise ToolError(f"{name} needs an instance context")
        sched_id = tool_input.get("schedule_id")
        if sched_id is None:
            raise ToolError("schedule_id is required")
        row = db.get_scheduled_command(int(sched_id))
        # scheduler.pause/remove take no instance_id themselves — this
        # ownership check is what stops one instance from touching
        # another's schedule via this tool, since scheduler.py's own
        # functions don't enforce that boundary at all.
        if row is None or row["instance_id"] != instance_id:
            raise ToolError(f"schedule {sched_id} not found")
        if name == "pause_schedule":
            scheduler.pause(int(sched_id))
        else:
            scheduler.remove(int(sched_id))
        return "ok"

    if name == "delegate_to_instance":
        from bot import agent_control, bot_instances, db
        from bot.backends.base import BackendError
        from bot.config import config
        from bot.router import router

        if instance_id is None:
            raise ToolError("delegate_to_instance needs an instance context")
        target_ref = tool_input.get("target_instance")
        prompt = (tool_input.get("prompt") or "").strip()
        if not target_ref or not prompt:
            raise ToolError("both target_instance and prompt are required")

        max_depth = config.current.get("agent_runtime", {}).get("max_delegation_depth", 2)
        depth = _delegation_depth.get()
        if depth >= max_depth:
            raise ToolError(
                f"delegation depth limit reached (depth={depth}, max_delegation_depth={max_depth}) — "
                "raise agent_runtime.max_delegation_depth in config/backends.yaml if deeper nesting is required"
            )

        target = agent_control.resolve_instance(target_ref)
        if target is None:
            raise ToolError(f"no bot instance found matching {target_ref!r}")
        if not agent_control.can_target(instance_id, target["id"]):
            raise ToolError(f"instance {instance_id} is not permitted to target {target['name']!r} under the current allowlist")

        source = bot_instances.get_instance(instance_id)
        source_name = source["name"] if source else f"instance {instance_id}"
        # Matches the ask_instance/dispatch_swarm_goal audit shape exactly
        # (found missing here while building the delegation-activity
        # dashboard panel — this call was invisible in that panel's audit-
        # log-backed data source until this was added) so all three
        # cross-instance delegation paths show up the same way.
        db.log_audit(actor=f"agent:{source_name}", action="agent_delegate", detail=f"-> {target['name']}: {prompt[:120]}")

        token = _delegation_depth.set(depth + 1)
        try:
            result = await router.ask(prompt, action_type="agent_delegate", instance_id=target["id"])
        except BackendError as exc:
            raise ToolError(f"delegation to {target['name']!r} failed: {exc}")
        finally:
            _delegation_depth.reset(token)
        return result.text

    if name == "spawn_subagent":
        from bot.agent_runtime import subagents
        from bot.backends.base import BackendError

        tasks = tool_input.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ToolError("tasks must be a non-empty array of {goal, output_schema?} objects")
        role = tool_input.get("role", "leaf")
        if role not in ("leaf", "orchestrator"):
            raise ToolError("role must be 'leaf' or 'orchestrator'")
        provider = tool_input.get("provider")
        model = tool_input.get("model")
        if bool(provider) != bool(model):
            raise ToolError("provider and model must both be given, or both omitted")
        max_children = tool_input.get("max_children")
        background = bool(tool_input.get("background", False))
        effort = tool_input.get("effort")
        try:
            result = await subagents.run_batch(
                tasks, role=role, provider=provider, model=model, effort=effort,
                max_children=max_children, parent_instance_id=instance_id,
                background=background,
            )
        except BackendError as exc:
            raise ToolError(str(exc))
        return _json_dumps(result)

    if name == "list_subagents":
        from bot.agent_runtime import subagent_registry

        dispatch_id = tool_input.get("dispatch_id")
        if dispatch_id:
            return _json_dumps(subagent_registry.describe(dispatch_id, parent_instance_id=instance_id))
        return _json_dumps(subagent_registry.describe_all(instance_id))

    if name == "steer_subagent":
        from bot.agent_runtime import subagent_registry

        dispatch_id = tool_input.get("dispatch_id")
        child_index = tool_input.get("child_index")
        message = (tool_input.get("message") or "").strip()
        if not dispatch_id or child_index is None or not message:
            raise ToolError("dispatch_id, child_index, and message are all required")
        subagent_registry.steer(dispatch_id, int(child_index), message, parent_instance_id=instance_id)
        return "steered"

    if name == "stop_subagent":
        from bot.agent_runtime import subagent_registry

        dispatch_id = tool_input.get("dispatch_id")
        child_index = tool_input.get("child_index")
        if not dispatch_id or child_index is None:
            raise ToolError("dispatch_id and child_index are both required")
        subagent_registry.stop(dispatch_id, int(child_index), parent_instance_id=instance_id)
        return "stopped"

    if name == "consult_models":
        from bot.agent_runtime import moa
        from bot.backends.base import BackendError

        question = (tool_input.get("question") or "").strip()
        references = tool_input.get("references")
        if not question:
            raise ToolError("question is required")
        if not isinstance(references, list) or not references:
            raise ToolError("references must be a non-empty array of {provider?, model} objects")
        aggregator = tool_input.get("aggregator")
        try:
            return await moa.consult(question, references, aggregator)
        except BackendError as exc:
            raise ToolError(str(exc))

    if name == "dispatch_batch_completions":
        from bot.agent_runtime import batches
        from bot.backends.base import BackendError

        tasks = tool_input.get("tasks")
        model = tool_input.get("model")
        if not isinstance(tasks, list) or not tasks:
            raise ToolError("tasks must be a non-empty array of {custom_id, goal} objects")
        if not model:
            raise ToolError("model is required")
        try:
            batch_id = await batches.submit(tasks, model=model, system_prompt=tool_input.get("system_prompt"))
        except BackendError as exc:
            raise ToolError(str(exc))
        return _json_dumps({"batch_id": batch_id})

    if name == "check_batch_status":
        from bot.agent_runtime import batches
        from bot.backends.base import BackendError

        batch_id = tool_input.get("batch_id")
        if not batch_id:
            raise ToolError("batch_id is required")
        try:
            return _json_dumps(await batches.status(batch_id))
        except BackendError as exc:
            raise ToolError(str(exc))

    if name == "get_batch_results":
        from bot.agent_runtime import batches
        from bot.backends.base import BackendError

        batch_id = tool_input.get("batch_id")
        if not batch_id:
            raise ToolError("batch_id is required")
        try:
            return _json_dumps(await batches.results(batch_id))
        except BackendError as exc:
            raise ToolError(str(exc))

    if name == "get_my_profile":
        from bot import bot_instances

        if instance_id is None:
            raise ToolError("get_my_profile needs an instance context")
        profile = bot_instances.render_profile_markdown(instance_id)
        if profile is None:
            raise ToolError(f"instance {instance_id} not found")
        return profile

    if name == "update_agent_config":
        from bot import agent_control, bot_instances

        if instance_id is None:
            raise ToolError("update_agent_config needs an instance context")
        target_ref = tool_input.get("target_instance")
        if not target_ref:
            raise ToolError("target_instance is required")
        target = agent_control.resolve_instance(target_ref)
        if target is None:
            raise ToolError(f"no bot instance found matching {target_ref!r}")
        if not agent_control.can_target(instance_id, target["id"]):
            raise ToolError(f"instance {instance_id} is not permitted to reconfigure {target['name']!r} under the current allowlist")

        fields = {
            k: tool_input[k]
            for k in ("name", "custom_instructions", "persona", "model", "can_target")
            if k in tool_input
        }
        if not fields:
            raise ToolError("nothing to update — pass at least one of name/custom_instructions/persona/model/can_target")
        try:
            bot_instances.update_instance(target["id"], actor=f"agent:{instance_id}", **fields)
        except bot_instances.ValidationError as exc:
            raise ToolError(str(exc))
        updated = bot_instances.render_profile_markdown(target["id"])
        return f"Updated {target['name']!r} (id {target['id']}).\n\n{updated}"

    if name == "admin_list_bot_instances":
        _require_admin(instance_id)
        from bot import bot_instances

        rows = []
        for row in bot_instances.list_instances():
            row = dict(row)
            row["credentials"] = _redact_credentials(row.get("credentials") or {})
            rows.append(row)
        return _json_dumps(rows)

    if name == "admin_get_bot_instance":
        _require_admin(instance_id)
        from bot import agent_control

        target_ref = tool_input.get("target_instance")
        if not target_ref:
            raise ToolError("target_instance is required")
        target = agent_control.resolve_instance(target_ref)
        if target is None:
            raise ToolError(f"no bot instance found matching {target_ref!r}")
        target = dict(target)
        target["credentials"] = _redact_credentials(target.get("credentials") or {})
        return _json_dumps(target)

    if name == "admin_create_bot_instance":
        _require_admin(instance_id)
        from bot import bot_instances

        required = ("name", "platform", "backend", "credentials", "allowed_user_ids")
        missing = [k for k in required if k not in tool_input]
        if missing:
            raise ToolError(f"missing required field(s): {missing}")
        try:
            new_id = bot_instances.create_instance(
                name=tool_input["name"], platform=tool_input["platform"], backend=tool_input["backend"],
                credentials=tool_input["credentials"], allowed_user_ids=tool_input["allowed_user_ids"],
                model=tool_input.get("model"), persona=tool_input.get("persona"),
                enabled=tool_input.get("enabled", True), actor=f"agent:{instance_id}",
            )
        except bot_instances.ValidationError as exc:
            raise ToolError(str(exc))
        created = dict(bot_instances.get_instance(new_id))
        created["credentials"] = _redact_credentials(created.get("credentials") or {})
        return f"Created bot instance {new_id}.\n\n{_json_dumps(created)}"

    if name == "admin_update_bot_instance":
        _require_admin(instance_id)
        from bot import agent_control, bot_instances

        target_ref = tool_input.get("target_instance")
        fields = tool_input.get("fields") or {}
        if not target_ref:
            raise ToolError("target_instance is required")
        if not fields:
            raise ToolError("fields must contain at least one field to update")
        target = agent_control.resolve_instance(target_ref)
        if target is None:
            raise ToolError(f"no bot instance found matching {target_ref!r}")
        try:
            bot_instances.update_instance(target["id"], actor=f"agent:{instance_id}", **fields)
        except bot_instances.ValidationError as exc:
            raise ToolError(str(exc))
        updated = dict(bot_instances.get_instance(target["id"]))
        updated["credentials"] = _redact_credentials(updated.get("credentials") or {})
        return f"Updated {target['name']!r} (id {target['id']}).\n\n{_json_dumps(updated)}"

    if name == "admin_delete_bot_instance":
        _require_admin(instance_id)
        from bot import agent_control, bot_instances

        target_ref = tool_input.get("target_instance")
        if not target_ref:
            raise ToolError("target_instance is required")
        target = agent_control.resolve_instance(target_ref)
        if target is None:
            raise ToolError(f"no bot instance found matching {target_ref!r}")
        if target["id"] == instance_id:
            raise ToolError("the admin instance cannot delete itself")
        if not tool_input.get("confirm"):
            raise ToolError("confirm=true is required to delete a bot instance")
        bot_instances.delete_instance(target["id"], actor=f"agent:{instance_id}")
        return f"Deleted bot instance {target['name']!r} (id {target['id']})."

    if name == "admin_set_default_backend":
        _require_admin(instance_id)
        from bot.router import set_default_backend

        backend = tool_input.get("backend")
        if not backend:
            raise ToolError("backend is required")
        try:
            result = set_default_backend(backend, actor=f"agent:{instance_id}")
        except ValueError as exc:
            raise ToolError(str(exc))
        return _json_dumps(result)

    if name == "admin_get_agent_settings":
        _require_admin(instance_id)
        from bot import agent_control, agent_settings

        target_ref = tool_input.get("target_instance")
        target_id = None
        if target_ref:
            target = agent_control.resolve_instance(target_ref)
            if target is None:
                raise ToolError(f"no bot instance found matching {target_ref!r}")
            target_id = target["id"]
        resolved = dict(agent_settings.get(target_id))
        resolved["is_admin_instance"] = "(hidden — dashboard/MCP only)"
        return _json_dumps(resolved)

    if name == "admin_set_agent_settings":
        _require_admin(instance_id)
        from bot import agent_control, agent_settings

        target_ref = tool_input.get("target_instance")
        fields = tool_input.get("fields") or {}
        target_id = None
        if target_ref:
            target = agent_control.resolve_instance(target_ref)
            if target is None:
                raise ToolError(f"no bot instance found matching {target_ref!r}")
            target_id = target["id"]
        if "is_admin_instance" in fields:
            raise ToolError("is_admin_instance can only be changed via the dashboard/MCP admin channel, never from a bot's own tool loop")
        try:
            result = agent_settings.set_settings(target_id, **fields)
        except ValueError as exc:
            raise ToolError(str(exc))
        result = dict(result)
        result["is_admin_instance"] = "(hidden — dashboard/MCP only)"
        return _json_dumps(result)

    if name == "admin_get_auto_manage_config":
        _require_admin(instance_id)
        from bot import agent_control, auto_manage

        target_ref = tool_input.get("target_instance")
        if not target_ref:
            raise ToolError("target_instance is required")
        target = agent_control.resolve_instance(target_ref)
        if target is None:
            raise ToolError(f"no bot instance found matching {target_ref!r}")
        return _json_dumps(auto_manage.get_config(target["id"]))

    if name == "admin_set_auto_manage_config":
        _require_admin(instance_id)
        from bot import agent_control, auto_manage

        target_ref = tool_input.get("target_instance")
        fields = tool_input.get("fields") or {}
        if not target_ref:
            raise ToolError("target_instance is required")
        target = agent_control.resolve_instance(target_ref)
        if target is None:
            raise ToolError(f"no bot instance found matching {target_ref!r}")
        return _json_dumps(auto_manage.set_config(target["id"], actor=f"agent:{instance_id}", **fields))

    if name == "admin_engage_estop":
        _require_admin(instance_id)
        from bot.agent_runtime import estop

        return _json_dumps(estop.engage(tool_input.get("reason"), actor=f"agent:{instance_id}"))

    if name == "admin_disengage_estop":
        _require_admin(instance_id)
        from bot.agent_runtime import estop

        return _json_dumps(estop.disengage(actor=f"agent:{instance_id}"))

    if name == "admin_get_estop_status":
        _require_admin(instance_id)
        from bot.agent_runtime import estop

        return _json_dumps(estop.status())

    if name == "admin_list_hooks":
        _require_admin(instance_id)
        from bot import db as _db

        rows = [dict(r) for r in _db.list_agent_hooks(event=tool_input.get("event"))]
        return _json_dumps(rows)

    if name == "admin_add_hook":
        _require_admin(instance_id)
        from bot import db as _db
        from bot.agent_runtime import hooks as _hooks

        event = tool_input.get("event")
        command = tool_input.get("command")
        if event not in _hooks.VALID_EVENTS:
            raise ToolError(f"event must be one of {sorted(_hooks.VALID_EVENTS)}")
        if not command:
            raise ToolError("command is required")
        target_instance_id = None
        target_ref = tool_input.get("target_instance")
        if target_ref:
            from bot import agent_control

            target = agent_control.resolve_instance(target_ref)
            if target is None:
                raise ToolError(f"no bot instance found matching {target_ref!r}")
            target_instance_id = target["id"]
        hook_id = _db.add_agent_hook(event, command, matcher=tool_input.get("matcher"), instance_id=target_instance_id)
        _db.log_audit(actor=f"agent:{instance_id}", action="hook_add", detail=f"hook {hook_id}: {event} -> {command!r}")
        return f"Added hook {hook_id} ({event})."

    if name == "admin_enable_hook":
        _require_admin(instance_id)
        from bot import db as _db

        hook_id = tool_input.get("hook_id")
        _db.set_agent_hook_enabled(hook_id, True)
        _db.log_audit(actor=f"agent:{instance_id}", action="hook_enable", detail=f"hook {hook_id}")
        return f"Enabled hook {hook_id}."

    if name == "admin_disable_hook":
        _require_admin(instance_id)
        from bot import db as _db

        hook_id = tool_input.get("hook_id")
        _db.set_agent_hook_enabled(hook_id, False)
        _db.log_audit(actor=f"agent:{instance_id}", action="hook_disable", detail=f"hook {hook_id}")
        return f"Disabled hook {hook_id}."

    if name == "admin_remove_hook":
        _require_admin(instance_id)
        from bot import db as _db

        hook_id = tool_input.get("hook_id")
        removed = _db.delete_agent_hook(hook_id)
        if not removed:
            raise ToolError(f"no such hook {hook_id}")
        _db.log_audit(actor=f"agent:{instance_id}", action="hook_remove", detail=f"hook {hook_id}")
        return f"Removed hook {hook_id}."

    if name == "admin_list_devices":
        _require_admin(instance_id)
        _require_elevated_device(device_tier)
        from bot import db as _db

        return _json_dumps([dict(r) for r in _db.list_devices()])

    if name == "admin_mint_device_key":
        _require_admin(instance_id)
        _require_elevated_device(device_tier)
        from bot import db as _db
        from bot import device_tiers

        label = tool_input.get("label")
        tier = tool_input.get("tier", "none")
        if not label:
            raise ToolError("label is required")
        if not device_tiers.is_valid_tier(tier):
            raise ToolError(f"unknown permission tier {tier!r}")
        if not device_tiers.can_mint(device_tier, tier):
            raise ToolError(f"this device's own tier ({device_tier}) can't mint a device at tier {tier!r}")
        key_id, plaintext = _db.create_api_key(label, permission_tier=tier)
        _db.log_audit(actor=f"agent:{instance_id}", action="mobile_key_create", detail=f"created key {key_id!r} ({label!r}) tier={tier!r}")
        return f"Minted device key {key_id} ({label!r}, tier={tier!r}). Plaintext key: {plaintext}"

    if name == "admin_set_device_tier":
        _require_admin(instance_id)
        _require_elevated_device(device_tier)
        from bot import db as _db
        from bot import device_tiers

        key_id = tool_input.get("key_id")
        new_tier = tool_input.get("tier")
        if not device_tiers.is_valid_tier(new_tier):
            raise ToolError(f"unknown permission tier {new_tier!r}")
        target = _db.get_api_key(key_id)
        if target is None:
            raise ToolError(f"no such device {key_id}")
        if not device_tiers.can_manage(device_tier, target["permission_tier"], is_self=False):
            raise ToolError("this device can only change the tier of a strictly lower-tier device")
        if not device_tiers.can_mint(device_tier, new_tier):
            raise ToolError(f"this device's own tier ({device_tier}) can't grant tier {new_tier!r}")
        _db.set_api_key_tier(key_id, new_tier)
        _db.log_audit(actor=f"agent:{instance_id}", action="mobile_key_set_tier", detail=f"set key {key_id} tier -> {new_tier!r}")
        return f"Set device {key_id}'s tier to {new_tier!r}."

    if name == "admin_revoke_device":
        _require_admin(instance_id)
        _require_elevated_device(device_tier)
        from bot import db as _db
        from bot import device_tiers

        key_id = tool_input.get("key_id")
        target = _db.get_api_key(key_id)
        if target is None:
            raise ToolError(f"no such device {key_id}")
        if not device_tiers.can_manage(device_tier, target["permission_tier"], is_self=False):
            raise ToolError("this device can only revoke a strictly lower-tier device")
        _db.revoke_api_key(key_id)
        _db.log_audit(actor=f"agent:{instance_id}", action="mobile_key_revoke", detail=f"revoked key {key_id}")
        return f"Revoked device {key_id}."

    if name == "admin_db_vacuum":
        _require_admin(instance_id)
        _require_elevated_device(device_tier)
        from bot import db as _db

        _db.vacuum(actor=f"agent:{instance_id}")
        return "Database vacuumed."

    if name == "admin_restore_snapshot":
        _require_admin(instance_id)
        _require_elevated_device(device_tier)
        from bot import db as _db
        from bot import snapshots

        snap_name = tool_input.get("name")
        if not snap_name:
            raise ToolError("name is required")
        try:
            snapshots.restore_snapshot(snap_name)
        except Exception as exc:
            raise ToolError(str(exc))
        _db.log_audit(actor=f"agent:{instance_id}", action="snapshot_restore", detail=snap_name)
        return f"Restored snapshot {snap_name!r}."

    if name == "admin_desktop_start":
        _require_admin(instance_id)
        _require_elevated_device(device_tier)
        from bot import db as _db
        from bot import desktop

        ok = desktop.start()
        _db.log_audit(actor=f"agent:{instance_id}", action="desktop_start", detail="")
        return f"Claude Desktop start {'succeeded' if ok else 'failed'}."

    if name == "admin_desktop_stop":
        _require_admin(instance_id)
        _require_elevated_device(device_tier)
        from bot import db as _db
        from bot import desktop

        ok = desktop.stop()
        _db.log_audit(actor=f"agent:{instance_id}", action="desktop_stop", detail="")
        return f"Claude Desktop stop {'succeeded' if ok else 'failed'}."

    if name == "admin_desktop_restart":
        _require_admin(instance_id)
        _require_elevated_device(device_tier)
        from bot import db as _db
        from bot import desktop

        desktop.restart()
        _db.log_audit(actor=f"agent:{instance_id}", action="desktop_restart", detail="")
        return "Claude Desktop restart requested."

    if name == "read_project_context":
        from bot import shared_context

        doc_name = (tool_input.get("name") or "").strip()
        if not doc_name:
            raise ToolError("name is required")
        try:
            doc = shared_context.read_doc(doc_name)
        except shared_context.SharedContextError as exc:
            raise ToolError(str(exc))
        if doc is None:
            return f"No shared context doc named {doc_name!r} yet — use write_project_context to create it."
        return doc["content"]

    if name == "write_project_context":
        from bot import shared_context

        doc_name = (tool_input.get("name") or "").strip()
        content = tool_input.get("content", "")
        if not doc_name:
            raise ToolError("name is required")
        actor = f"agent:{instance_id}" if instance_id is not None else "agent"
        try:
            shared_context.write_doc(doc_name, content, actor)
        except shared_context.SharedContextError as exc:
            raise ToolError(str(exc))
        return f"Saved shared context doc {doc_name!r} ({len(content)} chars)."

    if name == "list_project_context":
        from bot import shared_context

        docs = shared_context.list_docs()
        if not docs:
            return "(no shared context docs yet)"
        return "\n".join(f"- {d['name']} ({d['size']} chars, updated {d['updated_at']} by {d['updated_by']})" for d in docs)

    from bot import plugins as plugin_registry

    if plugin_registry.has_tool(name):
        try:
            return await plugin_registry.execute_tool(name, tool_input, workspace=workspace, instance_id=instance_id)
        except KeyError:
            pass

    from bot.agent_runtime import mcp_client

    if mcp_client.has_tool(name):
        return await mcp_client.call_tool(name, tool_input)

    raise ToolError(f"unknown tool {name!r}")

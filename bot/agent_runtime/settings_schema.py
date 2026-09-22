"""Every setting of the ABP agent, described once.

The native agent is configured from `config/backends.yaml` (the `native_agent:` block, plus `swarm_budget:`,
`swarm_observability:` and `agent_control:`). Those keys used to be reachable only by editing the YAML by hand. This
module lists each one with its label, help text, type, bounds and default, grouped into the tabs of the dashboard's
Agents page. The page renders its forms from this description, so a setting cannot exist without appearing there
(tests/test_agent_settings_schema.py fails if a key in the shipped config has no entry here), and the same rules
validate what the page saves.

A few settings are maps or nested structures that a form cannot express honestly (per-model limit overrides, LSP server
commands, MCP trust). They are listed in YAML_ONLY so the page can say where to edit them instead of hiding them.
"""
from __future__ import annotations

from typing import Any, Optional

from bot.agent_runtime import permissions, sandbox

TABS = [
    {"id": "runtime", "title": "Runtime", "description": "How an agent runs a turn: limits, instructions, context and tracing."},
    {"id": "safety", "title": "Safety", "description": "What an agent may do without asking, and where its commands run."},
    {"id": "tools", "title": "Tools", "description": "The web, a browser, code intelligence, skills and MCP."},
    {"id": "subagents", "title": "Sub-agents & swarms", "description": "Parallel workers, swarm spending limits and cross-bot control."},
    {"id": "models", "title": "Models", "description": "How model limits and free-tier allowances are enforced."},
]

# Settings that live in config/backends.yaml but are edited there, not on the page.
YAML_ONLY = [
    {"key": "native_agent.models.limits / native_agent.models.overrides", "why": "per-model limit tables (see docs/agents/models.md)"},
    {"key": "native_agent.context_windows", "why": "a map of model name to context size"},
    {"key": "native_agent.code_intel.lsp.servers / .formatters", "why": "language-server commands per language"},
    {"key": "native_agent.mcp_trust", "why": "a map of MCP server to trusted (also settable from the MCP tab)"},
    {"key": "native_agent.sandbox.docker", "why": "the docker image, network and resource limits"},
    {"key": "native_agent.sandbox.ssh", "why": "the ssh host, user, identity file and remote workspace root"},
    {"key": "native_agent.sandbox.wsl", "why": "the WSL distro name and extra wsl.exe arguments"},
    {"key": "native_agent.sandbox.windows_job", "why": "the job object's memory and process-count limits"},
]

_PERMISSION_MODE_HELP = {
    "default": "Ask before anything that changes something",
    "plan": "Read-only: the agent may look but not change anything",
    "accept_edits": "Edit files without asking; still ask for commands",
    "bypass": "Run everything without asking (needs 'Allow bypass mode')",
}


def _f(key: str, type: str, label: str, help: str, tab: str, section: str, default: Any, *, root: str = "native_agent",
       min: Optional[float] = None, max: Optional[float] = None, choices: Optional[list] = None, nullable: bool = False,
       advanced: bool = False, danger: str = "", danger_value: Any = True, applies: str = "next turn", unit: str = "") -> dict:
    return {"id": f"{root}.{key}", "root": root, "key": key, "type": type, "label": label, "help": help, "tab": tab,
            "section": section, "default": default, "min": min, "max": max, "choices": choices, "nullable": nullable,
            "advanced": advanced, "danger": danger, "danger_value": danger_value, "applies": applies, "unit": unit}


R, S, T, W, M = "runtime", "safety", "tools", "subagents", "models"

FIELDS: list[dict] = [
    # ------------------------------------------------------------------ runtime
    _f("limits.max_iterations", "int", "Steps per turn", "How many tool-use steps one message may take before the agent stops and reports back.", R, "Limits", 30, min=1, max=1000),
    _f("limits.max_seconds", "int", "Time limit per turn", "Stop a turn after this long. 0 means no limit.", R, "Limits", 0, min=0, max=86400, unit="seconds"),
    _f("limits.max_tokens", "int", "Token limit per turn", "Stop a turn after this many tokens have been spent on it. 0 means no limit.", R, "Limits", 0, min=0, unit="tokens"),
    _f("prompt.guidance", "bool", "Built-in guidance", "The operating guidance ABP gives every agent (how to use tools, how to be careful). Turn off only if you supply your own.", R, "Instructions", True),
    _f("prompt.environment", "bool", "Describe the environment", "Tell the agent the date, operating system, working folder and git state.", R, "Instructions", True),
    _f("prompt.extra", "textarea", "Extra instructions", "Text added to every agent's instructions, after the built-in guidance. Applies to all bots.", R, "Instructions", ""),
    _f("project_rules.enabled", "bool", "Read project instruction files", "Load AGENTS.md and CLAUDE.md from the working folder so the agent follows a project's own rules.", R, "Instructions", True),
    _f("project_rules.max_chars", "int", "Project instructions size cap", "Longest project instruction text to load.", R, "Instructions", 40000, min=1000, max=400000, advanced=True, unit="characters"),
    _f("context.compact_at", "float", "Condense the conversation at", "When this fraction of the model's context window is used, older tool output is condensed so the conversation keeps fitting.", R, "Context", 0.7, min=0.1, max=0.95),
    _f("context.keep_tool_results", "int", "Recent tool results always kept", "The most recent tool outputs are never condensed.", R, "Context", 6, min=0, max=100),
    _f("compression_threshold_chars", "int", "Shorten tool output longer than", "Very long tool output is shortened to this size before it goes back to the model. 0 turns it off.", R, "Context", 60000, min=0, unit="characters", advanced=True),
    _f("show_thinking_summary", "bool", "Show a summary of the agent's thinking", "Include the model's reasoning summary in replies where the model provides one.", R, "Behaviour", False),
    _f("trace.enabled", "bool", "Record run traces", "Keep a step-by-step record of each run, used for debugging, session export and trajectory export.", R, "Behaviour", True),
    _f("prompt_caching.enabled", "bool", "Prompt caching", "Reuse the unchanged start of each prompt to cut cost and latency, on models that support it.", R, "Behaviour", True),
    _f("prompt_caching.ttl", "enum", "Prompt cache lifetime", "How long a cached prompt is kept. The longer lifetime costs more to write.", R, "Behaviour", "5m", choices=[["5m", "5 minutes"], ["1h", "1 hour"]], advanced=True),
    # ------------------------------------------------------------------ safety
    _f("permissions.mode", "enum", "Default permission mode", "What an agent does when nothing else decides. Each bot may choose a stricter mode of its own.", S, "Approval", "default",
       choices=[[m, f"{m.replace('_', ' ').capitalize()} - {_PERMISSION_MODE_HELP[m]}"] for m in permissions.MODES]),
    _f("permissions.allow_bypass", "bool", "Allow bypass mode", "Lets a bot be set to bypass mode, where every tool runs without asking. Off means bypass is ignored everywhere.", S, "Approval", False,
       danger="Bypass mode lets an agent change files and run commands with no approval."),
    _f("permissions.locked", "bool", "Lock permissions to this page", "When on, per-bot permission modes and rules are ignored and cannot be changed, and a run can only become stricter.", S, "Approval", False,
       danger="Per-bot permission settings stop working while this is on."),
    _f("permissions.rules", "rules", "Permission rules", "One rule per line: decision, tool, then an optional pattern. Decisions are allow, ask and deny. Example: deny run_shell rm -rf*   (add ' # note' to explain it).", S, "Approval", []),
    _f("require_read_before_write", "bool", "Read a file before changing it", "An agent must read an existing file before it may overwrite or edit it, so it cannot clobber what it has not seen.", S, "Approval", True),
    _f("sandbox.backend", "enum", "Where commands run", "Local runs shell commands directly on this computer. Docker runs them in a container (needs Docker). SSH runs them on a remote host. WSL runs them in a Linux distro on this computer. Windows job confines the process tree without a container.", S, "Sandbox", "local",
       choices=[[b, {"local": "Local - on this computer", "docker": "Docker - in a container", "ssh": "SSH - on a remote host",
                     "wsl": "WSL - a Linux distro on this computer", "windows_job": "Windows job - confined, no container"}[b]] for b in sandbox.BACKENDS], applies="new sessions",
       danger="Local commands run with this computer's own access.", danger_value="local"),
    _f("sandbox.env.mode", "enum", "Environment passed to commands", "Which environment variables a command can see.", S, "Sandbox", "secrets",
       choices=[["secrets", "Secrets removed - everything except keys and tokens"], ["minimal", "Minimal - only what is needed to run"], ["inherit", "Inherit - everything, including keys"]], applies="new sessions",
       danger="Commands can read every key and token in this app's environment.", danger_value="inherit"),
    _f("mcp_pinning", "bool", "Pin MCP tool descriptions", "Block an MCP tool whose description or inputs change after you approved it, since that is how a tool can be turned against you.", S, "External tools", True),
    # ------------------------------------------------------------------ tools
    _f("web.enabled", "bool", "Web search and fetch", "Let agents search the web and read pages. Content from the web is treated as untrusted.", T, "Web", False),
    _f("web.allow_hosts", "list", "Only these sites", "If set, agents may reach only these hosts. One per line; blank means any public site.", T, "Web", []),
    _f("web.deny_hosts", "list", "Never these sites", "Hosts an agent may never reach. One per line.", T, "Web", []),
    _f("browser.enabled", "bool", "Browser", "Let agents drive a real browser (Edge or Chrome). Needs the browser extras installed.", T, "Browser", False),
    _f("browser.headless", "bool", "Hide the browser window", "Off shows the browser as the agent uses it.", T, "Browser", True),
    _f("browser.channel", "enum", "Browser to use", "Which browser to drive.", T, "Browser", "", choices=[["", "Automatic"], ["msedge", "Microsoft Edge"], ["chrome", "Google Chrome"]]),
    _f("browser.profile", "text", "Browser profile name", "A separate profile keeps the agent's logins and cookies apart from yours.", T, "Browser", "default", advanced=True),
    _f("browser.trusted_sites", "list", "Sites the agent may act on without asking", "One host per line.", T, "Browser", []),
    _f("browser.allow_private_hosts", "list", "Private addresses the browser may open", "Local or LAN hosts, one per line. Empty blocks all private addresses.", T, "Browser", [], advanced=True),
    _f("browser.max_elements", "int", "Page elements shown to the agent", "How many clickable elements of a page the agent is shown at once.", T, "Browser", 80, min=10, max=1000, advanced=True),
    _f("code_intel.lsp.enabled", "bool", "Code intelligence (language servers)", "After an edit, check the file with its language server and tell the agent about new errors.", T, "Code intelligence", False),
    _f("code_intel.lsp.wait_s", "int", "Wait for diagnostics", "How long to wait for a language server after an edit.", T, "Code intelligence", 4, min=1, max=120, unit="seconds", advanced=True),
    _f("code_intel.lsp.max_problems", "int", "Most problems reported", "Cap on how many problems are shown to the agent per edit.", T, "Code intelligence", 10, min=1, max=200, advanced=True),
    _f("code_intel.lsp.include_warnings", "bool", "Include warnings", "Report warnings as well as errors.", T, "Code intelligence", False, advanced=True),
    _f("server_tools.web_search", "bool", "Provider web search", "Use the model provider's own web search tool where the model has one.", T, "Provider-hosted tools", False),
    _f("server_tools.web_fetch", "bool", "Provider web fetch", "Use the provider's own page-fetch tool.", T, "Provider-hosted tools", False),
    _f("server_tools.code_execution", "bool", "Provider code execution", "Use the provider's own sandboxed code runner.", T, "Provider-hosted tools", False),
    _f("server_tools.tool_search", "bool", "Provider tool search", "Let the provider select from a large tool set on demand.", T, "Provider-hosted tools", False),
    _f("skills.allowed_hosts", "list", "Hosts skills may be fetched from", "Skill packs may only be installed from git hosts listed here, one per line. Empty allows none.", T, "Skills", []),
    _f("skills.trusted_keys", "list", "Trusted skill signing keys", "Public keys whose signed skill packs are installed without review, one per line.", T, "Skills", [], advanced=True),
    _f("skill_learning.enabled", "bool", "Learn skills from long tasks", "After a long, successful task the agent may draft a reusable skill. You review every draft before it is used.", T, "Skills", False),
    _f("skill_learning.min_tool_calls", "int", "Learn only from tasks with at least", "A task must use this many tool calls before a skill is drafted from it.", T, "Skills", 8, min=1, max=500, unit="tool calls", advanced=True),
    _f("mcp_sampling.enabled", "bool", "Let MCP servers ask the model", "Allow connected MCP servers to request completions from a model (sampling).", T, "MCP", False),
    _f("mcp_sampling.provider", "text", "Sampling provider", "The provider that answers MCP sampling requests. Blank uses none.", T, "MCP", None, nullable=True, advanced=True),
    _f("mcp_sampling.model", "text", "Sampling model", "The model that answers MCP sampling requests.", T, "MCP", None, nullable=True, advanced=True),
    # ------------------------------------------------------------------ sub-agents and swarms
    _f("max_concurrent_children", "int", "Sub-agents in parallel", "How many sub-agents one agent may run at the same time. A bot can override this in its own agent settings.", W, "Parallel sub-agents", 6, min=1, max=64),
    _f("max_global_background_children", "int", "Background jobs across all bots", "The most background sub-agents and shell jobs running at once, across every bot.", W, "Parallel sub-agents", 20, min=1, max=500, advanced=True),
    _f("enabled", "bool", "Swarm spending guard", "Check a swarm's estimated cost before it starts, and refuse or ask when it is too high.", W, "Swarm budget", True, root="swarm_budget"),
    _f("max_children", "int", "Most workers in one swarm", "A swarm asking for more workers than this is refused.", W, "Swarm budget", 6, root="swarm_budget", min=1, max=200),
    _f("max_estimated_usd", "float", "Most a swarm may cost", "A swarm estimated to cost more than this is refused.", W, "Swarm budget", 0.25, root="swarm_budget", min=0, unit="USD"),
    _f("require_confirm_above_usd", "float", "Ask before a swarm costs more than", "Between this and the maximum, a person must confirm first.", W, "Swarm budget", 1.0, root="swarm_budget", min=0, unit="USD"),
    _f("deny_unpriced_paid_models", "bool", "Refuse paid models with no known price", "A swarm cannot use a paid model whose price is unknown, since its cost cannot be checked.", W, "Swarm budget", False, root="swarm_budget"),
    _f("assumed_tokens_per_child.input", "int", "Assumed input tokens per worker", "Used to estimate a swarm's cost before it runs.", W, "Swarm budget", 2000, root="swarm_budget", min=0, advanced=True, unit="tokens"),
    _f("assumed_tokens_per_child.output", "int", "Assumed output tokens per worker", "Used to estimate a swarm's cost before it runs.", W, "Swarm budget", 1000, root="swarm_budget", min=0, advanced=True, unit="tokens"),
    _f("live_tool_events", "bool", "Show live tool activity from workers", "Stream each worker's tool calls to the Swarms view while it runs.", W, "Swarm behaviour", True, root="swarm_observability"),
    _f("mode", "enum", "Who may command whom", "Trust all lets any bot ask or command any other. Allowlist lets a bot target only the bots listed in its own settings.", W, "Cross-bot control", "trust_all", root="agent_control",
       choices=[["trust_all", "Trust all - any bot may target any other"], ["allowlist", "Allowlist - only bots it is allowed to target"]]),
    # ------------------------------------------------------------------ models
    _f("models.enforce", "bool", "Enforce model limits", "Hold back a call that would cross a known per-minute or daily limit, and switch to a fallback when one is exhausted. Off keeps counting but never holds a call back.", M, "Limits", True),
    _f("models.max_wait_s", "int", "Longest wait for a limit to clear", "A call that would cross a per-minute limit waits up to this long instead of failing.", M, "Limits", 15, min=0, max=600, unit="seconds"),
    _f("models.timezone", "text", "Time zone for daily limits", "An IANA name such as America/New_York. Blank uses this computer's time zone.", M, "Limits", "", nullable=True),
    # ------------------------------------------------------------------ automatic routing
    _f("router.enabled", "bool", "Allow automatic model routing", "Let a bot whose model is set to \"auto\" actually use the model router to pick one. Off makes an \"auto\" bot fail with a clear error instead of picking anything.", M, "Automatic routing", True),
    _f("router.candidates", "list", "Models the router may pick from", "One \"provider/model\" per line. Blank uses every free model this app knows of (never an Anthropic model unless you list one here yourself).", M, "Automatic routing", []),
    _f("router.also", "list", "Also consider these models", "Extra \"provider/model\" candidates added to the automatic free-model list above. Only used when the list above is blank.", M, "Automatic routing", [], advanced=True),
    _f("router.auto_failover", "bool", "Automatic failover", "When a bot's model fails mid-turn, try more models from the router (beyond its own configured fallback model) before giving up.", M, "Automatic routing", False),
    _f("router.max_failover_hops", "int", "Failover attempts", "How many extra models to try, at most, when automatic failover is on.", M, "Automatic routing", 2, min=0, max=5, advanced=True),
]

BY_ID: dict[str, dict] = {f["id"]: f for f in FIELDS}


def _path(field: dict) -> tuple[str, ...]:
    return (field["root"], *field["key"].split("."))


def _lookup(tree: Any, path: tuple[str, ...]) -> Any:
    node = tree
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# ------------------------------------------------------------------- rules text
def rules_to_lines(rules: Any) -> list[str]:
    lines = []
    for r in rules or []:
        if isinstance(r, dict):
            line = f"{r.get('decision', '')} {r.get('tool', '')}".strip()
            if r.get("match"):
                line += f" {r['match']}"
            if r.get("note"):
                line += f"  # {r['note']}"
            lines.append(line)
    return lines


def lines_to_rules(lines: list[str]) -> list[dict]:
    out = []
    for raw in lines:
        text = str(raw).strip()
        if not text:
            continue
        note = ""
        if " # " in text:
            text, _, note = text.partition(" # ")
        parts = text.strip().split(None, 2)
        rule = {"decision": parts[0].lower() if parts else "", "tool": parts[1] if len(parts) > 1 else ""}
        if len(parts) > 2:
            rule["match"] = parts[2].strip()
        if note.strip():
            rule["note"] = note.strip()
        out.append(rule)
    return out


# ------------------------------------------------------------------- read
def current_values(cfg: dict) -> dict[str, Any]:
    """id -> the value in effect (the configured one, else the default). Rules come back as text lines."""
    values: dict[str, Any] = {}
    for f in FIELDS:
        raw = _lookup(cfg, _path(f))
        value = f["default"] if raw is None and not f["nullable"] else raw
        if f["type"] == "rules":
            value = rules_to_lines(raw)
        elif f["type"] == "list":
            value = [str(v) for v in (raw or [])]
        values[f["id"]] = value
    return values


def configured_ids(cfg: dict) -> list[str]:
    """Ids whose key is explicitly present in the config (so the page can mark them as changed from the default)."""
    return [f["id"] for f in FIELDS if _lookup(cfg, _path(f)) is not None]


def describe() -> dict:
    return {"tabs": TABS, "fields": FIELDS, "yaml_only": YAML_ONLY}


# ------------------------------------------------------------------- write
def validate(changes: dict[str, Any]) -> tuple[dict[tuple[str, ...], Any], dict[str, str]]:
    """Check `changes` (id -> value). Returns (config edits by path, errors by id). Nothing is applied here."""
    edits: dict[tuple[str, ...], Any] = {}
    errors: dict[str, str] = {}
    for fid, value in (changes or {}).items():
        f = BY_ID.get(fid)
        if f is None:
            errors[fid] = "unknown setting"
            continue
        try:
            edits[_path(f)] = _coerce(f, value)
        except ValueError as exc:
            errors[fid] = str(exc)
    return edits, errors


def _coerce(f: dict, value: Any) -> Any:
    kind = f["type"]
    if value is None or (isinstance(value, str) and value == "" and f["nullable"]):
        if f["nullable"]:
            return None
        if kind == "text":
            return ""
        raise ValueError("a value is required")
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be on or off")
        return value
    if kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        number = int(value) if kind == "int" else float(value)
        if kind == "int" and float(value) != number:
            raise ValueError("must be a whole number")
        if f["min"] is not None and number < f["min"]:
            raise ValueError(f"must be at least {f['min']:g}")
        if f["max"] is not None and number > f["max"]:
            raise ValueError(f"must be at most {f['max']:g}")
        return number
    if kind in ("text", "textarea"):
        if not isinstance(value, str):
            raise ValueError("must be text")
        if len(value) > 20000:
            raise ValueError("is too long (at most 20000 characters)")
        return value
    if kind == "enum":
        allowed = [c[0] for c in f["choices"]]
        if value not in allowed:
            raise ValueError("must be one of: " + ", ".join(repr(a) for a in allowed))
        return value
    if kind == "list":
        if isinstance(value, str):
            value = value.splitlines()
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("must be a list of text lines")
        items = [v.strip() for v in value if v.strip()]
        if len(items) > 200 or any(len(v) > 500 for v in items):
            raise ValueError("is too long (at most 200 entries of 500 characters)")
        return items
    if kind == "rules":
        if isinstance(value, str):
            value = value.splitlines()
        if not isinstance(value, list):
            raise ValueError("must be a list of rule lines")
        rules = lines_to_rules([v if isinstance(v, str) else "" for v in value]) if all(isinstance(v, str) for v in value) else [dict(v) for v in value]
        problems = permissions.validate_rules(rules)
        if problems:
            raise ValueError("; ".join(problems))
        return rules
    raise ValueError(f"unsupported setting type {kind!r}")


def apply(changes: dict[str, Any], actor: str = "dashboard") -> tuple[dict[str, Any], dict[str, str]]:
    """Validate and save. Nothing is written unless every change is valid. Returns (values now in effect, errors)."""
    from bot.config import config

    edits, errors = validate(changes)
    if errors:
        return current_values(config.current), errors
    config.set_values(edits, actor=actor)
    return current_values(config.current), {}


def reset(ids: list[str], actor: str = "dashboard") -> dict[str, Any]:
    """Put settings back to their defaults (writes the default, so the file stays explicit and commented)."""
    from bot.config import config

    changes = {}
    for fid in ids:
        f = BY_ID.get(fid)
        if f is not None:
            changes[fid] = rules_to_lines(f["default"]) if f["type"] == "rules" else f["default"]
    edits, errors = validate(changes)
    if edits:
        config.set_values(edits, actor=actor)
    return current_values(config.current)

"""Import a Hermes Agent or OpenClaw setup into ABP (roadmap P9).

    python -m abp_import hermes   [--source DIR] [--instance N] [--no-secrets] [--apply]
    python -m abp_import openclaw [--source DIR] [--instance N] [--no-secrets] [--apply]

A dry run unless `--apply`, like the other importers. These two products hold far more than permission rules, so
more comes across. The formats were read from real installs and from each product's own code (Hermes's source and
its OpenClaw migration script), not guessed from documentation. What maps to what:

| From | Becomes in ABP |
|---|---|
| model providers, and API keys in `.env` / `openclaw.json` | providers (config/providers.yaml); a key is matched to its provider through the models.dev catalog |
| the default and fallback models | models the router may pick (`native_agent.router.also`); never an Anthropic model, which ABP uses only when you choose it |
| MCP servers | external MCP servers, untrusted by default, as with every importer |
| the skill library | linked in place (`native_agent.skills.external_dirs`), not copied; the skills that ship with Hermes are left out |
| `SOUL.md` (and OpenClaw's `IDENTITY.md`) | a bot's custom instructions |
| `MEMORY.md` / `USER.md` (and OpenClaw's daily `memory/*.md`) | that bot's memories, approved, since you had already kept them |
| scheduled jobs that run a prompt | that bot's scheduled commands, **created paused** so nothing fires before you look |
| Telegram / Discord / Slack channels | bots on those platforms, **created switched off**: the same token must not be polled by two programs |
| a website blocklist, denied tools | deny rules |

A bot to hold the instructions, memories and jobs is created (an app-only bot, or the first chat bot), unless
`--instance N` names an existing one. Its own instructions are never overwritten.

**Never imported, and said so:** approval mode "off" or "auto" (running everything without asking), allow-lists of
commands approved in the other product (ABP asks per command), Hermes's gateway hooks (Python handlers for Hermes's
own events), script-only jobs, OAuth logins, and anything whose meaning could not be established. `--no-secrets`
imports no API key or chat token at all. Secret values are never printed.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .core import Plan, _load, translate_mcp

HERMES_MEMORY_DELIMITER = "\n§\n"
MAX_MEMORIES = 500
MAX_INSTRUCTIONS = 20_000
# Providers whose models.dev entry has no base URL (their SDK knows it); all speak the OpenAI chat API here.
KNOWN_BASE = {
    "openai": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
}
CHANNEL_ENV = {    # platform -> (ABP credential field -> env var), allowed-users env var
    "telegram": ({"bot_token": "TELEGRAM_BOT_TOKEN"}, "TELEGRAM_ALLOWED_USERS"),
    "discord": ({"bot_token": "DISCORD_BOT_TOKEN"}, "DISCORD_ALLOWED_USERS"),
    "slack": ({"bot_token": "SLACK_BOT_TOKEN", "app_token": "SLACK_APP_TOKEN"}, "SLACK_ALLOWED_USERS"),
}
OPENCLAW_TOOLS = {"exec": "run_shell", "bash": "run_shell", "read": "read_file", "write": "write_file", "edit": "edit_file",
                  "apply_patch": "apply_patch", "browser": "browser", "web_fetch": "web_fetch", "web_search": "web_search",
                  "process": "run_shell"}


# ---- shared helpers --------------------------------------------------------------------------------------------
def read_env(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        if key.strip() and value:
            out[key.strip()] = value
    return out


def _name(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(raw)).strip("-")[:64] or "imported"


def split_args(value: Any) -> list[str]:
    """MCP arguments given as one string (seen in a real Hermes config) or a list."""
    if isinstance(value, list):
        return [str(a) for a in value]
    if not value:
        return []
    text = str(value).strip()
    if text.startswith("["):                            # a JSON list written as a string (also seen in a real config)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(a) for a in parsed]
        except ValueError:
            pass
    parts = shlex.split(text, posix=False)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]


def _catalog() -> dict:
    try:
        from bot import model_catalog

        data = model_catalog._raw()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def add_provider(plan: Plan, name: str, base_url: str, api_key: Optional[str], *, origin: str,
                 protocol: str = "openai", catalog_id: Optional[str] = None) -> None:
    name = _name(name)
    if any(p["name"] == name for p in plan.providers):
        return
    if not base_url:
        plan.warnings.append(f"{origin}: provider {name!r} has no address; skipped")
        return
    plan.providers.append({"name": name, "base_url": base_url.rstrip("/"), "protocol": protocol, "api_key": api_key or None,
                           "catalog_id": catalog_id, "origin": origin})


def providers_from_env(plan: Plan, env: dict[str, str], origin: str, *, used: set[str]) -> None:
    """Keys like OPENROUTER_API_KEY become providers, matched through the models.dev catalog."""
    catalog = _catalog()
    by_var: dict[str, list[str]] = {}
    for pid, entry in catalog.items():
        for var in (entry.get("env") or []) if isinstance(entry, dict) else []:
            by_var.setdefault(str(var), []).append(pid)
    unmatched = []
    for var, value in sorted(env.items()):
        if var in used or not value or not re.search(r"(_API_KEY|_KEY|_TOKEN)$", var):
            continue
        candidates = by_var.get(var, [])
        guess = var.removesuffix("_API_KEY").lower().replace("_", "-")
        pid = guess if guess in candidates else (candidates[0] if len(candidates) == 1 else None)
        if pid is None:
            if not any(var.startswith(p) for p in ("TELEGRAM_", "DISCORD_", "SLACK_", "HERMES_", "WHATSAPP_", "SIGNAL_")):
                unmatched.append(var)
            continue
        used.add(var)
        if pid == "anthropic":
            plan.warnings.append(f"{origin}: an Anthropic key is not imported (ABP uses Claude only when you choose it; add "
                                 "ANTHROPIC_API_KEY to ABP's own .env if you want it)")
            continue
        base = (catalog.get(pid) or {}).get("api") or KNOWN_BASE.get(pid)
        if not base:
            plan.warnings.append(f"{origin}: {var} is for {pid}, which has no OpenAI-compatible address ABP knows; skipped")
            continue
        add_provider(plan, pid, base, value, origin=f"{origin} {var}", catalog_id=pid)
    if unmatched:
        plan.warnings.append(f"{origin}: keys not matched to a known provider, not imported: {', '.join(unmatched)}")


def channels_from_env(plan: Plan, env: dict[str, str], origin: str) -> None:
    for platform, (fields, allow_var) in CHANNEL_ENV.items():
        creds = {f: env.get(var, "") for f, var in fields.items()}
        if not any(creds.values()):
            continue
        if not all(creds.values()):
            plan.warnings.append(f"{origin}: the {platform} settings are incomplete ({', '.join(v for v in fields.values())}); skipped")
            continue
        users = [u.strip() for u in env.get(allow_var, "").split(",") if u.strip()]
        add_channel(plan, platform, creds, users, origin)


def add_channel(plan: Plan, platform: str, creds: dict, users: list, origin: str) -> None:
    if not users:
        plan.warnings.append(f"{origin}: the {platform} bot allows no users (an open bot); not imported. Name your user id(s) "
                             f"and add it on ABP's Bots page")
        return
    ids = [int(u) if platform in ("telegram", "discord") and str(u).lstrip("-").isdigit() else str(u) for u in users]
    plan.channels.append({"platform": platform, "credentials": creds, "allowed_user_ids": ids, "origin": origin})


def markdown_entries(text: str) -> list[str]:
    """Memory entries from free-form Markdown: each bullet or paragraph, prefixed with its headings (the reading
    OpenClaw's own migration uses). Code blocks and tables are skipped."""
    entries, headings, para = [], [], []

    def flush():
        if para:
            block = " ".join(para).strip()
            para.clear()
            if block:
                entries.append((" > ".join(headings) + ": " if headings else "") + block)
    in_code = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            flush()
            continue
        if in_code or (line.startswith("|") and line.endswith("|")):
            continue
        h = re.match(r"^(#{1,6})\s+(.*\S)$", line)
        if h:
            flush()
            del headings[len(h.group(1)) - 1:]
            if not re.search(r"\b(MEMORY|USER|SOUL|AGENTS|TOOLS|IDENTITY)\.md\b", h.group(2), re.I):
                headings.append(h.group(2))
            continue
        b = re.match(r"^(?:[-*]|\d+\.)\s+(.*\S)$", line)
        if b:
            flush()
            entries.append((" > ".join(headings) + ": " if headings else "") + b.group(1))
        elif not line:
            flush()
        else:
            para.append(line)
    flush()
    seen, out = set(), []
    for e in entries:
        k = " ".join(e.lower().split())
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def add_memories(plan: Plan, entries: list[str], kind: str, origin: str) -> None:
    fresh = [e.strip() for e in entries if e and e.strip()]
    room = MAX_MEMORIES - len(plan.memories)
    if len(fresh) > room:
        plan.warnings.append(f"{origin}: only the first {max(room, 0)} of {len(fresh)} memory entries are imported")
        fresh = fresh[:max(room, 0)]
    plan.memories.extend({"kind": kind, "content": e} for e in fresh)


def link_skills(plan: Plan, root: Path, *, exclude: frozenset = frozenset(), origin: str) -> None:
    from bot import skill_packs

    if not root.is_dir() or any(d["path"] == str(root) for d in plan.skill_dirs):
        return
    folders = [d for d in skill_packs._skill_folders(root, skill_packs.EXTERNAL_DEPTH) if d.name not in exclude]
    if folders:
        plan.skill_dirs.append({"path": str(root), "count": len(folders), "exclude": sorted(exclude)})
    else:
        plan.warnings.append(f"{origin}: no skills in {root}")


def cron_interval(expr: str, now: datetime) -> Optional[tuple[int, datetime]]:
    """(interval seconds, first run) for a cron expression that repeats at a fixed interval; None for anything else
    (ABP's schedules are intervals, and an approximation would fire at the wrong times)."""
    parts = expr.split()
    if len(parts) != 5 or parts[2:4] != ["*", "*"]:
        return None
    minute, hour, _, _, dow = parts

    def num(v: str, hi: int) -> Optional[int]:
        return int(v) if v.isdigit() and int(v) <= hi else None
    m = re.fullmatch(r"\*/(\d+)", minute)
    if m and hour == "*" and dow == "*":
        n = int(m.group(1))
        return (n * 60, now.replace(second=0, microsecond=0) + timedelta(minutes=n - now.minute % n)) if 0 < n < 60 and 60 % n == 0 else None
    mm = num(minute, 59)
    if mm is None:
        return None
    if hour == "*" and dow == "*":
        first = now.replace(minute=mm, second=0, microsecond=0)
        return 3600, first if first > now else first + timedelta(hours=1)
    m = re.fullmatch(r"\*/(\d+)", hour)
    if m and dow == "*":
        n = int(m.group(1))
        if not (0 < n < 24 and 24 % n == 0):
            return None
        first = now.replace(minute=mm, second=0, microsecond=0)
        while first <= now or first.hour % n:
            first += timedelta(hours=1)
        return n * 3600, first
    hh = num(hour, 23)
    if hh is None:
        return None
    first = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if dow == "*":
        return 86400, first if first > now else first + timedelta(days=1)
    day = num(dow, 7)
    if day is None:
        return None
    target = (day % 7 + 6) % 7                       # cron: 0 or 7 = Sunday; Python: Monday = 0
    first += timedelta(days=(target - first.weekday()) % 7)
    return 604800, first if first > now else first + timedelta(days=7)


def _instructions(parts: list[tuple[str, str]]) -> str:
    text = "\n\n".join(f"{body.strip()}" for label, body in parts if body and body.strip())
    return text[:MAX_INSTRUCTIONS]


# ---- Hermes ------------------------------------------------------------------------------------------------------
def hermes_home(home: Path, source: Optional[Path]) -> tuple[Optional[Path], list[Path]]:
    """Where Hermes keeps its data, resolved the way Hermes does (HERMES_HOME, then %LOCALAPPDATA%\\hermes on
    Windows, then ~/.hermes), plus any other Hermes folders found, which are reported."""
    if source is not None:
        return (source if (source / "config.yaml").is_file() else None), []
    real_home = Path.home()
    candidates = []
    if os.environ.get("HERMES_HOME", "").strip() and home == real_home:
        candidates.append(Path(os.path.expandvars(os.environ["HERMES_HOME"])).expanduser())
    local = os.environ.get("LOCALAPPDATA") if home == real_home else None
    candidates.append(Path(local) / "hermes" if local else home / "AppData" / "Local" / "hermes")
    candidates.append(home / ".hermes")
    found = [c for c in dict.fromkeys(candidates) if (c / "config.yaml").is_file()]
    return (found[0] if found else None), found[1:]


def hermes(project: Path, home: Path, source: Optional[Path] = None) -> Plan:
    plan = Plan("Hermes")
    root, others = hermes_home(home, source)
    if root is None:
        plan.warnings.append("no Hermes configuration found (looked for config.yaml in HERMES_HOME, %LOCALAPPDATA%\\hermes "
                             "and ~/.hermes); use --source DIR")
        return plan
    for other in others:
        plan.warnings.append(f"another Hermes folder exists at {other}; import it with --source {other}")
    import yaml

    cfg_path = root / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        plan.warnings.append(f"could not read {cfg_path}: {exc}")
        return plan
    plan.files.append(str(cfg_path))
    env_path = root / ".env"
    env = read_env(env_path)
    if env:
        plan.files.append(str(env_path))
    used: set[str] = set()

    # Providers: config `providers` and `custom_providers`, the default model's endpoint, then keys in .env.
    def key_for(spec: dict) -> Optional[str]:
        var = str(spec.get("key_env") or spec.get("api_key_env") or "")
        if var:
            used.add(var)
        return spec.get("api_key") or (env.get(var) if var else None)
    for name, spec in (cfg.get("providers") or {}).items():
        if isinstance(spec, dict):
            add_provider(plan, name, str(spec.get("base_url") or ""), key_for(spec), origin="config.yaml providers")
            if spec.get("model"):
                plan.router_also.append(f"{_name(name)}/{spec['model']}")
    for spec in cfg.get("custom_providers") or []:
        if isinstance(spec, dict) and spec.get("name"):
            add_provider(plan, spec["name"], str(spec.get("base_url") or ""), key_for(spec), origin="config.yaml custom_providers")
    model = cfg.get("model")
    if isinstance(model, dict):
        prov, default = str(model.get("provider") or ""), str(model.get("default") or model.get("model") or "")
        if model.get("base_url") and prov:
            add_provider(plan, prov, str(model["base_url"]), key_for(model), origin="config.yaml model",
                         protocol="responses" if "responses" in str(model.get("api_mode") or "") else "openai")
        if prov and default:
            plan.router_also.append(f"{_name(prov)}/{default}")
    elif isinstance(model, str) and "/" in model:
        plan.router_also.append(model)
    for fb in cfg.get("fallback_providers") or []:
        if isinstance(fb, dict) and fb.get("provider") and fb.get("model"):
            plan.router_also.append(f"{_name(fb['provider'])}/{fb['model']}")
    providers_from_env(plan, env, ".env", used=used)
    known = {p["name"] for p in plan.providers}
    kept = []
    for ref in dict.fromkeys(plan.router_also):
        head = ref.split("/", 1)[0]
        if head.lower() == "anthropic" or "claude" in ref.lower():
            plan.warnings.append(f"the model {ref} is not added to the router: ABP uses Claude only when you choose it")
        elif head in known:
            kept.append(ref)
        else:
            plan.warnings.append(f"the model {ref} is not added to the router: its provider {head!r} was not imported")
    plan.router_also = kept
    if (root / "auth.json").is_file():
        plan.warnings.append("OAuth logins (auth.json, e.g. the Nous Portal) are not imported; sign in again in ABP if needed")

    # MCP servers: Hermes allows `args` as one string.
    servers = {}
    for name, spec in (cfg.get("mcp_servers") or {}).items():
        if not isinstance(spec, dict):
            continue
        spec = dict(spec, args=split_args(spec.get("args")))
        if spec.get("tools"):
            plan.warnings.append(f"MCP server {name!r} limits which tools it offers; ABP offers them all (approve or deny them there)")
        servers[name] = spec
    translate_mcp(servers, plan, "config.yaml mcp_servers")

    # Safety settings: only what narrows.
    approvals = cfg.get("approvals") or {}
    if str(approvals.get("mode") or "").lower() == "off":
        plan.warnings.append("Hermes approvals were off; that is not imported (ABP asks before anything that changes something)")
    if cfg.get("command_allowlist"):
        plan.warnings.append(f"{len(cfg['command_allowlist'])} command patterns you approved in Hermes are not imported; ABP asks "
                             "per command (add allow rules on the ABP Agents page if you want some)")
    block = ((cfg.get("security") or {}).get("website_blocklist")) or {}
    if block.get("enabled"):
        for domain in block.get("domains") or []:
            rule = {"decision": "deny", "tool": "web_fetch", "match": str(domain), "note": "imported from Hermes"}
            if rule not in plan.rules:
                plan.rules.append(rule)
    backend = str((cfg.get("terminal") or {}).get("backend") or "local")
    if backend != "local":
        plan.warnings.append(f"Hermes ran commands with the {backend!r} backend; ABP's equivalent is Settings -> Safety -> "
                             "Sandbox (not changed by an import)")
    hook_dirs = sorted(p.name for p in (root / "hooks").iterdir() if p.is_dir()) if (root / "hooks").is_dir() else []
    if hook_dirs or cfg.get("hooks"):
        plan.warnings.append("Hermes hooks are not imported: they are Python handlers for Hermes's own events"
                             + (f" ({', '.join(hook_dirs)})" if hook_dirs else ""))
    if cfg.get("personalities"):
        plan.warnings.append("Hermes personalities are not imported; SOUL.md is")

    # Skills: the library, linked in place; skills that ship with Hermes describe Hermes's own tools and are left out.
    skills = root / "skills"
    bundled = frozenset()
    manifest = skills / ".bundled_manifest"
    if manifest.is_file():
        bundled = frozenset(line.split(":", 1)[0].strip() for line in manifest.read_text(encoding="utf-8-sig").splitlines()
                            if line.strip())
    link_skills(plan, skills, exclude=bundled, origin="skills")
    for extra in ((cfg.get("skills") or {}).get("external_dirs")) or []:
        link_skills(plan, Path(os.path.expandvars(str(extra))).expanduser(), origin="skills.external_dirs")

    # The agent: SOUL.md, memories, jobs, channels.
    soul = _read(root / "SOUL.md")
    if soul:
        plan.files.append(str(root / "SOUL.md"))
        plan.agent = {"name": "Hermes", "instructions": _instructions([("SOUL.md", soul)])}
    for fname, kind in (("MEMORY.md", "fact"), ("USER.md", "user")):
        text = _read(root / "memories" / fname)
        if text:
            plan.files.append(str(root / "memories" / fname))
            add_memories(plan, [e for e in text.split(HERMES_MEMORY_DELIMITER)], kind, f"memories/{fname}")
    _hermes_jobs(plan, root / "cron" / "jobs.json")
    channels_from_env(plan, env, ".env")
    if (plan.memories or plan.schedules) and not plan.agent:
        plan.agent = {"name": "Hermes", "instructions": ""}
    return plan


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _hermes_jobs(plan: Plan, path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    plan.files.append(str(path))
    jobs = data.get("jobs") if isinstance(data, dict) else data
    now = datetime.now().astimezone()
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or job.get("id") or "job")
        if job.get("no_agent") or job.get("script"):
            plan.warnings.append(f"job {name!r} runs a script, not a prompt; ABP's schedules run prompts, so it is not imported")
            continue
        prompt = str(job.get("prompt") or "").strip()
        if not prompt:
            plan.warnings.append(f"job {name!r} has no prompt; skipped")
            continue
        sched = job.get("schedule") or {}
        kind = sched.get("kind")
        if kind == "interval" and int(sched.get("minutes") or 0) > 0:
            interval, first = int(sched["minutes"]) * 60, now + timedelta(minutes=int(sched["minutes"]))
        elif kind == "cron" and (conv := cron_interval(str(sched.get("expr") or ""), now)):
            interval, first = conv
        else:
            plan.warnings.append(f"job {name!r} ({sched.get('display') or kind}) does not repeat at a fixed interval; not imported")
            continue
        times = (job.get("repeat") or {}).get("times")
        deliver = str(job.get("deliver") or "")
        platform, _, chat = deliver.partition(":")
        plan.schedules.append({"name": name, "prompt": prompt, "interval_s": interval,
                               "first_run_at": first.astimezone(timezone.utc).isoformat(timespec="seconds"),
                               "platform": platform if chat else "", "chat_id": chat, "max_runs": times,
                               "skills": list(job.get("skills") or [])})


# ---- OpenClaw ----------------------------------------------------------------------------------------------------
def openclaw_home(home: Path, source: Optional[Path]) -> Optional[Path]:
    candidates = [source] if source is not None else [home / ".openclaw", home / ".clawdbot", home / ".moltbot"]
    if source is None and os.environ.get("OPENCLAW_STATE_DIR", "").strip() and home == Path.home():
        candidates.insert(0, Path(os.environ["OPENCLAW_STATE_DIR"]).expanduser())
    for c in candidates:
        if any((c / n).is_file() for n in ("openclaw.json", "clawdbot.json", "moltbot.json")):
            return c
    return None


def _secret(value: Any, env: dict[str, str]) -> Optional[str]:
    """OpenClaw's SecretInput: a plain string, "${VAR}", or {"source": "env", "id": VAR}. File/exec sources are not
    read (they run programs or read files ABP should not touch)."""
    if isinstance(value, str):
        m = re.fullmatch(r"\$\{(\w+)\}", value.strip())
        return (env.get(m.group(1)) if m else value.strip()) or None
    if isinstance(value, dict) and value.get("source") == "env":
        return env.get(str(value.get("id") or "")) or None
    return None


def _field(cfg: dict, name: str) -> Any:
    if cfg.get(name) is not None:
        return cfg[name]
    default = (cfg.get("accounts") or {}).get("default")
    return default.get(name) if isinstance(default, dict) else None


def openclaw(project: Path, home: Path, source: Optional[Path] = None) -> Plan:
    plan = Plan("OpenClaw")
    root = openclaw_home(home, source)
    if root is None:
        plan.warnings.append("no OpenClaw configuration found (looked for ~/.openclaw/openclaw.json); use --source DIR")
        return plan
    cfg_path = next(root / n for n in ("openclaw.json", "clawdbot.json", "moltbot.json") if (root / n).is_file())
    cfg = _load(cfg_path)
    if cfg is None:
        plan.warnings.append(f"could not read {cfg_path} (JSON5 features beyond comments and trailing commas are not supported)")
        return plan
    plan.files.append(str(cfg_path))
    env = read_env(root / ".env")
    env.update({str(k): str(v) for k, v in (((cfg.get("env") or {}).get("vars")) or {}).items() if v})
    used: set[str] = set()

    for name, spec in ((cfg.get("models") or {}).get("providers") or {}).items():
        if not isinstance(spec, dict):
            continue
        api = str(spec.get("api") or spec.get("apiType") or "openai-completions")
        raw_key = spec.get("apiKey")
        if isinstance(raw_key, dict) and raw_key.get("source") in ("file", "exec"):
            plan.warnings.append(f"provider {name!r} reads its key from a {raw_key['source']}; add the key in ABP yourself")
        if isinstance(raw_key, str) and (m := re.fullmatch(r"\$\{(\w+)\}", raw_key.strip())):
            used.add(m.group(1))
        if api.startswith("openai"):
            add_provider(plan, name, str(spec.get("baseUrl") or ""), _secret(raw_key, env), origin="models.providers",
                         protocol="responses" if api == "openai-responses" else "openai")
        else:
            plan.warnings.append(f"provider {name!r} uses the {api!r} API, which ABP does not speak (it needs an "
                                 "OpenAI-compatible address); skipped")
    model = ((cfg.get("agents") or {}).get("defaults") or {}).get("model")
    refs = [model] if isinstance(model, str) else ([model.get("primary")] + list(model.get("fallbacks") or [])
                                                   if isinstance(model, dict) else [])
    providers_from_env(plan, env, "OpenClaw keys", used=used)
    known = {p["name"] for p in plan.providers}
    for ref in [r for r in refs if r]:
        head = str(ref).split("/", 1)[0]
        if head.lower() == "anthropic" or "claude" in str(ref).lower():
            plan.warnings.append(f"the model {ref} is not added to the router: ABP uses Claude only when you choose it")
        elif head in known:
            plan.router_also.append(str(ref))
        else:
            plan.warnings.append(f"the model {ref} is not added to the router: its provider {head!r} was not imported")

    translate_mcp(((cfg.get("mcp") or {}).get("servers")) or {}, plan, "mcp.servers")

    tools = cfg.get("tools") or {}
    for tool in tools.get("deny") or []:
        abp = OPENCLAW_TOOLS.get(str(tool).lower())
        if abp is None:
            plan.warnings.append(f"denied tool {tool!r} has no ABP equivalent; skipped")
            continue
        rule = {"decision": "deny", "tool": abp, "match": "", "note": "imported from OpenClaw"}
        if rule not in plan.rules:
            plan.rules.append(rule)
    exec_mode = str((((cfg.get("approvals") or {}).get("exec")) or {}).get("mode") or (cfg.get("approvals") or {}).get("mode") or "")
    if exec_mode == "auto":
        plan.warnings.append("OpenClaw ran commands without asking; that is not imported (ABP asks before anything that "
                             "changes something)")
    if (root / "exec-approvals.json").is_file():
        plan.warnings.append("commands you approved in OpenClaw (exec-approvals.json) are not imported; ABP asks per command")
    if cfg.get("cron") or (root / "cron").is_dir():
        plan.warnings.append("OpenClaw scheduled jobs are not imported: their format was not checked against a real install")
    if cfg.get("hooks"):
        plan.warnings.append("OpenClaw hooks are not imported: they are handlers for OpenClaw's own events")

    # Workspace: persona, memories, skills.
    ws_cfg = str(((cfg.get("agents") or {}).get("defaults") or {}).get("workspace") or "").strip()
    workspaces = ([Path(os.path.expandvars(ws_cfg)).expanduser()] if ws_cfg else []) + \
        [root / n for n in ("workspace", "workspace-main", "workspace.default")]
    ws = next((w for w in workspaces if w.is_dir()), None)
    if ws is not None:
        parts = [(n, _read(ws / n)) for n in ("SOUL.md", "IDENTITY.md")]
        if any(body for _, body in parts):
            plan.files += [str(ws / n) for n, body in parts if body]
            plan.agent = {"name": "OpenClaw", "instructions": _instructions(parts)}
        for n in ("AGENTS.md", "TOOLS.md", "HEARTBEAT.md"):
            if (ws / n).is_file():
                plan.warnings.append(f"{n} is not imported: it describes OpenClaw's own workspace conventions")
        user = _read(ws / "USER.md")
        if user:
            add_memories(plan, markdown_entries(user), "user", "USER.md")
        facts = [_read(ws / "MEMORY.md")] + [_read(p) for p in sorted((ws / "memory").glob("*.md"))] if (ws / "memory").is_dir() \
            else [_read(ws / "MEMORY.md")]
        entries = [e for text in facts if text for e in markdown_entries(text)]
        if entries:
            add_memories(plan, entries, "fact", "MEMORY.md and memory/")
        link_skills(plan, ws / "skills", origin="workspace skills")
    link_skills(plan, root / "skills", origin="managed skills")

    channels = cfg.get("channels") or {}
    tg = channels.get("telegram") if isinstance(channels.get("telegram"), dict) else {}
    tg_users = list(_field(tg, "allowFrom") or []) if tg else []
    allow_file = root / "credentials" / "telegram-default-allowFrom.json"
    if allow_file.is_file():
        try:
            tg_users += list(json.loads(allow_file.read_text(encoding="utf-8")).get("allowFrom") or [])
        except (OSError, ValueError):
            pass
    specs = {"telegram": (tg, {"bot_token": "botToken"}, tg_users),
             "discord": (channels.get("discord") or {}, {"bot_token": "token"}, None),
             "slack": (channels.get("slack") or {}, {"bot_token": "botToken", "app_token": "appToken"}, None)}
    for platform, (ch, fields, users) in specs.items():
        if not isinstance(ch, dict) or not ch:
            continue
        creds = {f: _secret(_field(ch, k), env) or "" for f, k in fields.items()}
        if not all(creds.values()):
            plan.warnings.append(f"the {platform} channel's token is missing or stored outside openclaw.json; skipped")
            continue
        allowed = users if users is not None else list(_field(ch, "allowFrom") or [])
        add_channel(plan, platform, creds, [str(u) for u in dict.fromkeys(allowed)], f"channels.{platform}")
    for other in sorted(set(channels) - set(specs)):
        plan.warnings.append(f"the {other} channel is not imported; set it up on ABP's Bots page")
    if (plan.memories or plan.schedules) and not plan.agent:
        plan.agent = {"name": "OpenClaw", "instructions": ""}
    return plan


# ---- apply -------------------------------------------------------------------------------------------------------
def _unique_bot_name(base: str) -> str:
    from bot import bot_instances

    taken = {i["name"] for i in bot_instances.list_instances()}
    name, n = base, 2
    while name in taken:
        name, n = f"{base} ({n})", n + 1
    return name


def apply_extra(plan: Plan, *, instance_id: Optional[int] = None, with_secrets: bool = True) -> dict:
    """Write the Hermes / OpenClaw parts of a plan. Nothing that exists is overwritten."""
    from bot import bot_instances, db, memory, providers
    from bot.config import config

    done = {"providers": 0, "router_models": 0, "skill_libraries": 0, "bots": 0, "memories": 0, "schedules": 0}
    actor = f"import:{plan.source}"
    existing = providers.list_providers()
    for p in plan.providers:
        if p["name"] in existing:
            plan.warnings.append(f"provider {p['name']!r} already exists in ABP; left as it is")
            continue
        providers.set_provider(p["name"], p["base_url"], protocol=p["protocol"], api_key=p["api_key"] if with_secrets else None,
                               catalog_id=p.get("catalog_id"), actor=actor)
        done["providers"] += 1
    na = config.current.get("native_agent") or {}
    if plan.router_also:
        current = list(((na.get("router") or {}).get("also")) or [])
        added = [r for r in plan.router_also if r not in current]
        if added:
            config.set_values({("native_agent", "router", "also"): current + added}, actor=actor)
            done["router_models"] = len(added)
    if plan.skill_dirs:
        current = list(((na.get("skills") or {}).get("external_dirs")) or [])
        paths = {str(c.get("path") if isinstance(c, dict) else c) for c in current}
        added = [({"path": d["path"], "exclude": d["exclude"]} if d.get("exclude") else d["path"])
                 for d in plan.skill_dirs if d["path"] not in paths]
        if added:
            config.set_values({("native_agent", "skills", "external_dirs"): current + added}, actor=actor)
            done["skill_libraries"] = len(added)

    # The bot(s): channel bots switched off; otherwise one app-only bot, unless --instance names one.
    instructions = (plan.agent or {}).get("instructions") or ""
    target = instance_id
    if target is not None:
        row = bot_instances.get_instance(target)
        if row is None:
            raise ValueError(f"there is no bot instance {target}")
        if instructions and not (row.get("custom_instructions") or "").strip():
            bot_instances.update_instance(target, actor=actor, custom_instructions=instructions)
        elif instructions:
            plan.warnings.append(f"bot {target} already has instructions; the imported ones were not written")
    existing_bots = bot_instances.list_instances()
    for ch in plan.channels if with_secrets else []:
        base = f"{plan.agent['name'] if plan.agent else plan.source} on {ch['platform'].capitalize()}"
        same = next((b for b in existing_bots if b["platform"] == ch["platform"]
                     and (b.get("credentials") or {}).get("bot_token") == ch["credentials"].get("bot_token")), None)
        if same is not None:                          # imported before (or set up by hand): use it, never a second poller
            plan.warnings.append(f"a {ch['platform']} bot with that token already exists ({same['name']}); used it")
            target = target if target is not None else same["id"]
            continue
        try:
            new_id = bot_instances.create_instance(
                name=_unique_bot_name(base), platform=ch["platform"], backend="native_agent", credentials=ch["credentials"],
                allowed_user_ids=ch["allowed_user_ids"], enabled=False, model="auto",
                custom_instructions=instructions or None, actor=actor)
        except (bot_instances.ValidationError, ValueError) as exc:
            plan.warnings.append(f"the {ch['platform']} bot was not created: {exc}")
            continue
        done["bots"] += 1
        target = target if target is not None else new_id
    if plan.channels and not with_secrets:
        plan.warnings.append("chat channels were not imported (--no-secrets)")
    if target is None and plan.agent:
        earlier = next((b for b in existing_bots if b["platform"] == "app" and b["name"].startswith(plan.agent["name"])
                        and (b.get("custom_instructions") or "") == instructions), None)
        if earlier is not None:
            target = earlier["id"]
    if target is None and plan.agent:
        target = bot_instances.create_instance(
            name=_unique_bot_name(plan.agent["name"]), platform="app", backend="native_agent", credentials={},
            allowed_user_ids=[], enabled=True, model="auto", custom_instructions=instructions or None, actor=actor)
        done["bots"] += 1
    if target is None:
        return done
    for m in plan.memories:
        r = memory.remember_full(target, m["content"], source="user", kind=m["kind"])
        if not r["approved"]:
            memory.approve(r["id"])
        done["memories"] += 0 if r["duplicate"] else 1
    have = {(j["prompt"], j["interval_s"]) for j in db.list_scheduled_commands(target)}
    for s in plan.schedules:
        if (s["prompt"], s["interval_s"]) in have:
            continue
        chat = s["chat_id"] or "import"
        sid = db.create_scheduled_command(target, chat, "cron", s["prompt"], s["interval_s"], s["first_run_at"],
                                          max_runs=s.get("max_runs"))
        db.set_scheduled_command_enabled(sid, False)
        done["schedules"] += 1
    done["bot_id"] = target
    return done

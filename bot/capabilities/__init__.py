"""ABP's capability index (PLAN-local-chat.md P5): everything ABP can do, in one searchable list, so a person (Local Chat's
`/` palette, Settings search), the agent (the read-only `abp_capabilities` tool) and the docs all answer "how do I X in ABP"
from the same place.

Two parts:

* the **static index** - every page, user-visible action, API route, module and settings section - is generated from the
  sources (both UIs' navigation, action-registry.js, docs/api/openapi.json, the built-in module manifests, settings-hub.js)
  into docs/capabilities.json and docs/capabilities.md by `python scripts/gen_capabilities.py`. tests/test_capabilities.py
  fails when a route, page, module or action is missing from it (regenerate), like the hot-reload classification;
* **live entries** added when asked: the agent's tools, the configured MCP servers and Local Chat's own commands.

Each entry: {"id", "kind": page|action|route|module|setting|tool|mcp|command, "label", "desc", "where" (how to get there:
"#page", "action:<id>", "GET /api/...", "settings/<section>", "tool:<name>"), "network": needs the internet, "cloud": uses a
cloud model / service, "keywords"}.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

KINDS = ("page", "action", "route", "module", "setting", "tool", "mcp", "command")
# Areas whose features need the internet, or a cloud model or service (the rest work offline on this machine).
NETWORK_HINTS = ("tailscale", "hosting", "updates", "browser", "providers", "provider", "hermes", "telegram", "discord", "slack",
                 "whatsapp", "servers", "federation", "mobile", "push", "pull", "web_fetch", "web_search", "cloud", "github",
                 "transferd", "transferdaemon", "release", "funnel", "marketplace")
CLOUD_HINTS = ("cloud", "providers", "provider", "claude", "anthropic", "hermes", "setup", "openai", "capacity")

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "static": None}


def root() -> Path:
    return Path(__file__).resolve().parents[2]


def _flags(text: str) -> tuple[bool, bool]:
    t = text.lower()
    return any(h in t for h in NETWORK_HINTS), any(h in t for h in CLOUD_HINTS)


def _entry(eid: str, kind: str, label: str, desc: str, where: str, keywords: str = "") -> dict:
    net, cloud = _flags(f"{eid} {label} {where} {keywords}")
    return {"id": eid, "kind": kind, "label": label, "desc": desc, "where": where, "network": net, "cloud": cloud,
            "keywords": " ".join(keywords.split())}


# ---- the sources ---------------------------------------------------------------------------------------------------------------
_NAV = re.compile(r'<a href="#([\w-]+)"([^>]*)>(.*?)</a>', re.S)


def _pages(base: Path) -> list[dict]:
    """Every page in either UI's navigation (the desktop app's labels and keywords first)."""
    seen: dict[str, dict] = {}
    for page in ("desktop-app/ui/index.html", "bot/dashboard/static/dashboard.html"):
        html = (base / page).read_text(encoding="utf-8")
        start = html.index('<nav class="sidenav"')
        nav = html[start:html.index("</nav>", start)]
        for pid, attrs, inner in _NAV.findall(nav):
            if pid in seen:
                continue
            label = re.sub(r"\s+", " ", re.sub(r"<small>.*?</small>", "", re.sub(r'<span class="ic">.*?</span>', "", inner, flags=re.S), flags=re.S))
            label = re.sub(r"<[^>]+>", "", label).replace("&amp;", "&").strip()
            small = re.search(r"<small>(.*?)</small>", inner, re.S)
            kw = re.search(r'data-kw="([^"]*)"', attrs)
            seen[pid] = _entry(f"page:{pid}", "page", label, (small.group(1).replace("&amp;", "&").strip() if small else ""),
                               f"#{pid}", kw.group(1) if kw else "")
    return list(seen.values())


_Q = r"'((?:\\'|[^'])*)'"
_ACTION = re.compile(r"\{ id: " + _Q + r", label: " + _Q + r", group: " + _Q)
_SCREEN = re.compile(r"\[\[" + _Q + r"(?:, " + _Q + r")?\], " + _Q + r", " + _Q + r"\]")


def _actions(base: Path) -> list[dict]:
    js = (base / "desktop-app/ui/action-registry.js").read_text(encoding="utf-8")
    out = []
    for aid, label, group in _ACTION.findall(js):
        out.append(_entry(f"action:{aid}", "action", label.replace("\\'", "'"), f"{group} action", f"action:{aid}", group))
    for first, _second, label, token in _SCREEN.findall(js):
        aid = "nav." + re.sub(r"[^a-z0-9]+", "-", first)
        out.append(_entry(f"action:{aid}", "action", f"Go to {label}", f"Go to action (shortcut g {token})", f"action:{aid}", "go to navigate"))
    return out


def _routes(base: Path) -> list[dict]:
    spec = json.loads((base / "docs/api/openapi.json").read_text(encoding="utf-8"))
    out = []
    for path, ops in sorted(spec.get("paths", {}).items()):
        for method, op in sorted(ops.items()):
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            area = path.split("/")[2] if path.count("/") >= 2 else ""
            out.append(_entry(f"route:{method.upper()} {path}", "route", op.get("summary") or path, f"API ({area})",
                              f"{method.upper()} {path}", area))
    return out


def _modules() -> list[dict]:
    from bot.modules import manifest as mf
    from bot.modules import registry

    out = []
    for d in registry.BUILTIN:
        m = mf.parse(d)
        out.append(_entry(f"module:{m.id}", "module", m.name or m.id, (m.description or "")[:300], "#modules", f"module {m.id}"))
    return out


_SECTION = re.compile(r"\{ id: " + _Q + r", title: " + _Q + r", desc: " + _Q)
_ITEM = re.compile(r"\[" + _Q + r", " + _Q + r", " + _Q + r"(?:, " + _Q + r")?\]")


def _settings(base: Path) -> list[dict]:
    js = (base / "desktop-app/ui/settings-hub.js").read_text(encoding="utf-8")
    body = js[js.index("const SECTIONS = ["):js.index("const REDIRECTS")]
    out = []
    parts = _SECTION.split(body)
    # parts: [prefix, id, title, desc, rest, id, title, desc, rest, ...]
    for i in range(1, len(parts) - 3, 4):
        sid, title, desc, rest = parts[i], parts[i + 1].replace("\\'", "'"), parts[i + 2].replace("\\'", "'"), parts[i + 3]
        out.append(_entry(f"setting:{sid}", "setting", f"Settings: {title}", desc, f"settings/{sid}", "settings preferences"))
        for label, idesc, target, _alt in _ITEM.findall(rest):
            out.append(_entry(f"setting:{sid}/{label}", "setting", f"{label.replace(chr(92) + chr(39), chr(39))} ({title})",
                              idesc.replace("\\'", "'"), f"settings/{sid}", target))
    return out


def generate(base: Optional[Path] = None) -> dict:
    """The static index from the sources (deterministic: sorted, no timestamps)."""
    base = base or root()
    entries = _pages(base) + _actions(base) + _routes(base) + _modules() + _settings(base)
    entries.sort(key=lambda e: (KINDS.index(e["kind"]), e["id"]))
    counts = {k: sum(1 for e in entries if e["kind"] == k) for k in KINDS if any(e["kind"] == k for e in entries)}
    return {"about": "Generated by scripts/gen_capabilities.py from the UIs, action-registry.js, docs/api/openapi.json, the "
                     "built-in module manifests and settings-hub.js. Do not edit by hand.", "counts": counts, "entries": entries}


def markdown(doc: dict) -> str:
    """docs/capabilities.md: the same index, readable."""
    lines = ["# ABP capability index", "", "Everything ABP can do, generated from the sources by `python scripts/gen_capabilities.py` "
             "(do not edit by hand). Local Chat's `/` palette, Settings search and the agent's `abp_capabilities` tool search "
             "this list plus the live agent tools and MCP servers. *net* = needs the internet; *cloud* = uses a cloud model or "
             "service; everything else works offline on this machine.", ""]
    lines += ["| Kind | Count |", "|---|---|"] + [f"| {k} | {n} |" for k, n in doc["counts"].items()] + [""]
    titles = {"page": "Pages", "action": "Actions (command palette, shortcuts)", "setting": "Settings", "module": "Modules",
              "route": "API routes"}
    for kind in ("page", "setting", "action", "module"):
        rows = [e for e in doc["entries"] if e["kind"] == kind]
        lines += [f"## {titles[kind]}", "", "| Name | What | Where | |", "|---|---|---|---|"]
        for e in rows:
            tag = " ".join(t for t, on in (("net", e["network"]), ("cloud", e["cloud"])) if on)
            lines.append(f"| {e['label']} | {e['desc']} | `{e['where']}` | {tag} |".replace("\n", " "))
        lines.append("")
    lines += [f"## {titles['route']}", ""]
    by_area: dict[str, list[dict]] = {}
    for e in (e for e in doc["entries"] if e["kind"] == "route"):
        by_area.setdefault(e["keywords"] or "-", []).append(e)
    for area, rows in sorted(by_area.items()):
        lines += [f"### {area} ({len(rows)})", ""] + [f"- `{e['where']}` {e['label']}" for e in rows] + [""]
    return "\n".join(lines).rstrip() + "\n"


def write(base: Optional[Path] = None) -> list[Path]:
    base = base or root()
    doc = generate(base)
    jp, mp = base / "docs/capabilities.json", base / "docs/capabilities.md"
    jp.write_text(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    mp.write_text(markdown(doc), encoding="utf-8", newline="\n")
    forget()
    return [jp, mp]


# ---- reading and searching -----------------------------------------------------------------------------------------------------
def static_entries() -> list[dict]:
    with _lock:
        if _cache["static"] is None:
            try:
                _cache["static"] = json.loads((root() / "docs/capabilities.json").read_text(encoding="utf-8"))["entries"]
            except (OSError, ValueError, KeyError):
                _cache["static"] = []
        return list(_cache["static"])


def forget() -> None:
    with _lock:
        _cache["static"] = None


def live_entries() -> list[dict]:
    """The agent's tools, the configured MCP servers and Local Chat's own commands (they change with config and units)."""
    out: list[dict] = []
    try:
        from bot.agent_runtime import tools as _tools  # noqa: F401 - importing registers the self-registering tools
        from bot.agent_runtime import toolspec

        schemas = {s.get("name"): s for s in list(getattr(_tools, "TOOL_SCHEMAS", [])) + toolspec.registered_schemas()}
        for name in sorted(set(toolspec.builtin_names()) | set(toolspec.registered_names())):
            sc = schemas.get(name) or {}
            spec = toolspec.spec_for(name)
            out.append(_entry(f"tool:{name}", "tool", name, str(sc.get("description") or "")[:300], f"tool:{name}",
                              f"agent tool {spec.permission}" + (" read-only" if spec.read_only else "")))
    except Exception:  # noqa: BLE001 - the index still answers without the tools
        pass
    try:
        from bot.config import config

        for name in sorted(((config.current.get("mcp") or {}).get("servers") or {}) if isinstance(config.current.get("mcp"), dict) else []):
            out.append(_entry(f"mcp:{name}", "mcp", f"MCP server {name}", "a configured MCP server", "#control", "mcp server"))
    except Exception:  # noqa: BLE001
        pass
    for cmd, desc in (("/new", "Start a new Local Chat session"), ("/model", "Open the model picker"),
                      ("/mode", "Switch the agent type: chat, build, plan, review, factory"), ("/autonomy", "Set autonomy 0-5"),
                      ("/cloud", "Set cloud assist: off, ask, auto"), ("/export", "Download the session"), ("/settings", "Open Settings")):
        out.append(_entry(f"command:{cmd}", "command", cmd, desc, "#local-chat", "local chat command"))
    return out


def index(*, live: bool = True) -> list[dict]:
    return static_entries() + (live_entries() if live else [])


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"how", "do", "i", "the", "a", "an", "to", "in", "abp", "of", "for", "can", "my", "is", "what", "where", "and", "on", "with", "me"}


def search(query: str, *, kinds: Optional[list[str]] = None, limit: int = 20, offline: bool = False, live: bool = True) -> list[dict]:
    """Entries ranked by how well they match the words of the query (label weighs most, then keywords, then the
    description). Routes rank below pages, settings and actions for the same match: a person usually wants the screen."""
    words = [w for w in _WORD.findall((query or "").lower()) if w not in _STOP]
    rows = [e for e in index(live=live) if (not kinds or e["kind"] in kinds) and not (offline and (e["network"] or e["cloud"]))]
    if not words:
        return rows[:limit]
    weight = {"page": 1.3, "setting": 1.25, "action": 1.2, "command": 1.1, "module": 1.0, "tool": 0.9, "mcp": 0.9, "route": 0.6}
    scored = []
    for e in rows:
        label, kw, desc = e["label"].lower(), e["keywords"].lower(), f"{e['desc']} {e['where']}".lower()
        score = 0.0
        for w in words:
            if re.search(rf"\b{re.escape(w)}", label):
                score += 3
            elif re.search(rf"\b{re.escape(w)}", kw):
                score += 2
            elif w in desc:
                score += 1
        if score:
            scored.append((score * weight.get(e["kind"], 1.0), e))
    scored.sort(key=lambda t: (-t[0], t[1]["label"]))
    return [e for _, e in scored[: max(1, limit)]]


def stats() -> dict:
    rows = index()
    return {"total": len(rows), "by_kind": {k: sum(1 for e in rows if e["kind"] == k) for k in KINDS}, "at": time.time()}

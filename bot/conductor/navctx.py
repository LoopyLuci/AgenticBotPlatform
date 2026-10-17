"""Navigation and memory context for the conductor's local models (LF-INT): capped slices instead of whole files.

    planning_context(root, goal)      repo map + context_pack for the planner
    task_pack(root, text)             the context_pack pre-seeded into a worker prompt (the worker caches it per task)
    vault_block(query)                what the context vault remembers about the task (earlier findings, outcomes)
    capture(...)                      reviewer feedback and accepted outcomes go to the vault, so a retry carries them
    usage(root) / usage_delta(...)    codeintel call counts, to report whether a task navigated

Everything is best effort: when codeintel cannot index or the vault is off, the function returns "" / {} and the conductor
behaves as before. `conductor.context.use_codeintel` / `use_vault` switch the two off.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("bot.conductor.navctx")

NAV_TOOLS = ("outline", "find_symbol", "read_symbol", "callers", "related_tests", "context_pack")
RECALL_TOOL = "context_recall"
DEFAULTS: dict[str, Any] = {"use_codeintel": True, "use_vault": True, "pack_tokens": 2500, "plan_pack_tokens": 3000,
                            "repo_map_tokens": 1500, "plan_context_chars": 9000, "vault_tokens": 500}


def settings(cfg: Any = None) -> dict[str, Any]:
    """The `conductor.context` settings over their defaults (cfg = the conductor settings dict, or None to read them)."""
    if cfg is None:
        from bot import conductor

        cfg = conductor.settings()
    own = cfg.get("context") if isinstance(cfg, dict) else None
    return {**DEFAULTS, **(own if isinstance(own, dict) else {})}


def tool_names(cfg: Any = None) -> list[str]:
    s = settings(cfg)
    return (list(NAV_TOOLS) if s["use_codeintel"] else []) + ([RECALL_TOOL] if s["use_vault"] else [])


def planning_context(root: Path, goal: str, cfg: Any = None) -> str:
    s = settings(cfg)
    if not s["use_codeintel"]:
        return ""
    parts: list[str] = []
    try:
        from bot.agent_runtime import repo_map

        text = repo_map.cached_build(Path(root), int(s["repo_map_tokens"]))
        if text and text.strip():
            parts.append("REPOSITORY MAP (files and their main symbols):\n" + text.strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug("repo map unavailable: %s", exc)
    pack = task_pack(root, goal, int(s["plan_pack_tokens"]), cfg)
    if pack:
        parts.append("CODE RELEVANT TO THE GOAL:\n" + pack)
    return "\n\n".join(parts)


def task_pack(root: Path, text: str, budget: int = 0, cfg: Any = None) -> str:
    s = settings(cfg)
    if not s["use_codeintel"] or not str(text or "").strip():
        return ""
    try:
        from bot.codeintel import tools

        res = tools.context_pack(Path(root), str(text)[:2000], int(budget or s["pack_tokens"]))
        return str(res.text or "").strip()
    except Exception as exc:  # noqa: BLE001 - no index means the old behaviour
        logger.debug("context_pack unavailable: %s", exc)
        return ""


def vault_block(query: str, cfg: Any = None) -> str:
    s = settings(cfg)
    if not s["use_vault"] or not str(query or "").strip():
        return ""
    try:
        from bot import context_vault
        from bot.context_vault import tools as vtools

        vault = context_vault.get_vault()
        if vault is None:
            return ""
        items = vault.recall(str(query)[:1000], k=6, budget_tokens=int(s["vault_tokens"]))
        return vtools.format_block(items) if items else ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("vault recall unavailable: %s", exc)
        return ""


def capture(text: str, kind: str, project: str, source: str = "conductor", cfg: Any = None) -> None:
    """Remember a finding (kind "review") or an accepted outcome (kind "task_state") in the vault."""
    if not settings(cfg)["use_vault"] or not str(text or "").strip():
        return
    try:
        from bot import context_vault

        vault = context_vault.get_vault()
        if vault is not None:
            vault.add(str(text)[:1500], kind, source=source, project=project, tags=("conductor",))
    except Exception as exc:  # noqa: BLE001
        logger.debug("vault capture failed: %s", exc)


def usage(root: Path) -> dict[str, dict]:
    try:
        from bot.codeintel import tools

        return tools.usage(Path(root))
    except Exception:  # noqa: BLE001
        return {}


def usage_delta(before: dict, after: dict) -> dict[str, dict]:
    out = {}
    for tool, row in (after or {}).items():
        was = (before or {}).get(tool) or {}
        calls, tokens = row.get("calls", 0) - was.get("calls", 0), row.get("tokens", 0) - was.get("tokens", 0)
        if calls > 0:
            out[tool] = {"calls": calls, "tokens": tokens}
    return out


def tool_mix(tool_calls: list) -> dict[str, Any]:
    """Counts per tool from an agent run, and whether it navigated (any codeintel tool, or context_pack pre-seeded)."""
    counts: dict[str, int] = {}
    for c in tool_calls or []:
        name = c.get("tool") if isinstance(c, dict) else None
        if name:
            counts[name] = counts.get(name, 0) + 1
    nav = sum(n for t, n in counts.items() if t in NAV_TOOLS)
    return {"counts": dict(sorted(counts.items())), "navigation_calls": nav, "skipped_navigation": nav == 0}


def tracked_files(root: Path) -> set[str]:
    """Files git tracks in `root` (empty set when that cannot be read: validation is then skipped)."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, timeout=30, check=True).stdout
    except Exception:  # noqa: BLE001
        return set()
    return {ln.strip().replace("\\", "/") for ln in out.splitlines() if ln.strip()}


def unknown_paths(paths: list[str], known: set[str]) -> list[str]:
    """Paths that are neither tracked files nor new files inside a directory that exists (those are invented)."""
    if not known:
        return []
    dirs = {""} | {"/".join(f.split("/")[:i]) for f in known for i in range(1, len(f.split("/")))}
    return [p for p in paths if p not in known and "/".join(p.split("/")[:-1]) not in dirs]

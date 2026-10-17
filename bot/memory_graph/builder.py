"""Build one episode from a finished run: join the trace (tools, tokens, model, status) with the handoff pack (goal, files,
test status, last error), write the episode and bump the graph.

    on_run_end(trace)            - the hook agent_runtime.trace.Trace.end() calls; never raises
    build_episode(run_id, ...)   - the join itself (replay a trace + pack in tests, or backfill)

Edges written (n counts every sighting; wins/fails follow the episode outcome):
    tool  --helped-->  task_type        skill --helped--> task_type
    model --used_in--> task_type        file  --used_in--> task_type
    file --co_occurred--> file          symbol --co_occurred--> file      (codeintel; only the names, its own refs are joined at query time)
    tool  --caused-->  error            error --resolved_by--> fix        (tool errors; the context vault's error_fix items)
Everything optional (pack, codeintel, vault) degrades to "less graph", never to an exception.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from bot.memory_graph import settings as mg_settings
from bot.memory_graph import tasktype
from bot.memory_graph.store import Graph, db_path

logger = logging.getLogger("bot.memory_graph")

SUCCESS_AT = 0.6          # an episode with score >= this counts as a win for its edges
_DIGITS = re.compile(r"\d+")
_PATHISH = re.compile(r"[A-Za-z]:[\\/][^\s'\"]*|(?:/[\w.\-]+){2,}")


def error_signature(text: str) -> str:
    """A stable short key for an error: lower-cased, numbers and paths flattened."""
    t = _PATHISH.sub("<path>", str(text or "").lower())
    return _DIGITS.sub("#", " ".join(t.split()))[:80]


def redact(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    try:
        from bot.context_vault import redact as vr

        text = vr.sanitize(text) or ""
    except Exception:  # noqa: BLE001
        pass
    return text[:limit]


def outcome_of(run_status: str, pack: Any) -> dict:
    """{status, test_status, score}: how the run ended, refined by the pack's test status when there is one."""
    tests = ((getattr(pack, "test_status", None) or {}).get("status") or "unknown") if pack is not None else "unknown"
    pack_status = getattr(pack, "status", "") if pack is not None else ""
    if run_status not in ("ok", "running") or pack_status in ("failed", "interrupted"):
        status, score = "failed", 0.1 if tests == "passing" else 0.0
    elif tests == "passing":
        status, score = "ok", 1.0
    elif tests == "failing":
        status, score = "partial", 0.3
    else:
        status, score = "ok", 0.7
    return {"status": status, "test_status": tests, "score": score}


def _rel(root: Optional[Path], path: str) -> str:
    p = str(path).replace("\\", "/").strip()
    if root is not None:
        try:
            return Path(p).resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return p.lstrip("./") if not re.match(r"^[A-Za-z]:|^/", p) else ""      # an absolute path outside the repo is machine-local


def _tools_from(events: list[dict]) -> tuple[dict, list[str], list[dict]]:
    tools: dict[str, dict] = {}
    skills: list[str] = []
    errors: list[dict] = []
    for e in events:
        if e["kind"] != "tool.call":
            continue
        d = e["data"]
        name = str(d.get("tool") or "")
        if not name:
            continue
        t = tools.setdefault(name, {"calls": 0, "errors": 0})
        t["calls"] += 1
        if d.get("status") == "failed":
            t["errors"] += 1
            if d.get("error"):
                errors.append({"tool": name, "error": str(d["error"])[:300]})
        if d.get("skill") and d["skill"] not in skills:
            skills.append(str(d["skill"]))
    return tools, skills, errors


def _symbols(root: Path, files: list[str], cfg: dict) -> list[tuple[str, str]]:
    """[(file, symbol qname)] from the codeintel index; indexes just those files if they are missing. [] on any trouble."""
    out: list[tuple[str, str]] = []
    try:
        from bot.codeintel import indexer, store

        conn = store.connect(root)
        try:
            for rel in files:
                if conn.execute("SELECT 1 FROM ci_files WHERE path=?", (rel,)).fetchone() is None:
                    if not (Path(root) / rel).is_file():
                        continue
                    indexer.update(Path(root), only=[rel])
                    conn.commit()
                rows = conn.execute("SELECT qname FROM ci_symbols WHERE file=? AND kind IN ('class','function','method') "
                                    "ORDER BY depth, end_line - start_line DESC LIMIT ?",
                                    (rel, int(cfg["max_symbols_per_file"]))).fetchall()
                out += [(rel, r["qname"]) for r in rows if r["qname"]]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - no codeintel, no symbols
        logger.debug("memory graph: codeintel lookup failed", exc_info=True)
    return out


def _fix_for(error: str) -> str:
    """The Fix half of the context vault's best error_fix item for this error, or ""."""
    try:
        from bot import context_vault

        vault = context_vault.get_vault()
        if vault is None:
            return ""
        for it in vault.recall(error, None, ["error_fix"], 1, 300):
            m = re.search(r"Fix:\s*(.+)", str(it.get("text") or ""), re.S)
            if m:
                return redact(m.group(1), 160)
    except Exception:  # noqa: BLE001
        logger.debug("memory graph: vault fix lookup failed", exc_info=True)
    return ""


def build_episode(run_id: str, *, trace_store: Any = None, pack: Any = None, task_id: str = "", goal_hint: str = "",
                  workspace: Any = None, graph: Optional[Graph] = None, cfg: Optional[dict] = None,
                  project: str = "") -> Optional[dict]:
    """Write the episode for one run (idempotent: a run already recorded is returned as it is). None if the run is unknown."""
    from bot.agent_runtime import trace as tr

    cfg = cfg or mg_settings.load()
    store = trace_store or tr.get_store()
    events = tr.run_events(run_id, store)
    if not events:
        return None
    graph = graph or Graph(db_path(getattr(store, "path", None)))
    existing = graph.get_episode(run_id)
    if existing is not None:
        return existing
    start = next((e for e in events if e["kind"] == "agent.run.start"), {"data": {}})["data"]
    end = next((e for e in reversed(events) if e["kind"] == "agent.run.end"), {"data": {}})["data"]
    llm = [e["data"] for e in events if e["kind"] == "llm.call"]
    tools, skills, tool_errors = _tools_from(events)
    task_id = task_id or str(end.get("task_id") or "")
    if not tools and not task_id and pack is None:
        return None                       # plain chat: no tools, no task - nothing to learn from
    if pack is None and task_id:
        try:
            from bot.capacity import handoff

            pack = handoff.read(task_id)
        except Exception:  # noqa: BLE001
            pack = None
    root = Path(str(workspace or getattr(pack, "workspace", "") or "")) if (workspace or getattr(pack, "workspace", "")) else None
    if root is not None and not root.is_dir():
        root = None
    goal = (getattr(pack, "goal", "") if pack is not None else "") or goal_hint
    if pack is not None and not goal:
        goal = " ".join(str(n) for n in (pack.notes or [])[:2])
    task_type = str(start.get("task_type") or "") or tasktype.classify(goal)
    proj = project or (re.split(r"[\\/]", str(root).rstrip("\\/"))[-1][:80] if root is not None else "")
    files = list(dict.fromkeys(f for f in (_rel(root, f) for f in (getattr(pack, "files_touched", None) or [])) if f))
    files = files[:int(cfg["max_files"])]
    errors = list(tool_errors)
    last_err = getattr(pack, "last_error", "") if pack is not None else ""
    if last_err:
        errors.append({"tool": "", "error": redact(last_err, 300)})
    created = float(next((e["ts"] for e in reversed(events) if e["kind"] == "agent.run.end"), time.time()))
    outcome = outcome_of(str(end.get("status") or "ok"), pack)
    tokens = sum(int(d.get("tokens") or 0) for d in llm)
    usd_known = [float(d["usd"]) for d in llm if d.get("usd") is not None]
    model = str(start.get("model") or (llm[0].get("model") if llm else "") or "")
    ep = {"id": run_id, "created": created, "project": proj, "task_type": task_type, "goal_summary": redact(goal, 400),
          "tools": tools, "skills": skills, "files": files,
          "errors": [{"tool": e["tool"], "sig": error_signature(e["error"])} for e in errors[:10]],
          "model": model, "tokens": tokens, "usd": round(sum(usd_known), 6) if usd_known else None,
          "iterations": int(end.get("iterations") or len(llm)), "duration_ms": int(end.get("duration_ms") or 0),
          "outcome": outcome, "run_id": run_id, "task_id": task_id}
    won = outcome["score"] >= SUCCESS_AT
    edges: list[tuple] = [("tool", t, "helped", "task_type", task_type, won) for t in tools]
    edges += [("skill", s, "helped", "task_type", task_type, won) for s in skills]
    if model:
        edges.append(("model", model, "used_in", "task_type", task_type, won))
    for f in files:
        edges.append(("file", f, "used_in", "task_type", task_type, won))
    for i, a in enumerate(files):
        edges += [("file", a, "co_occurred", "file", b, None) for b in files[i + 1:]]
    if root is not None and files and cfg.get("codeintel", True):
        edges += [("symbol", f"{f}::{q}", "co_occurred", "file", f, None) for f, q in _symbols(root, files, cfg)]
    seen_sigs: set[str] = set()
    for e in errors[:10]:
        sig = error_signature(e["error"])
        if not sig or sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        if e["tool"]:
            edges.append(("tool", e["tool"], "caused", "error", sig, False))
        if won and cfg.get("vault_fixes", True):
            fix = _fix_for(e["error"])
            if fix:
                edges.append(("error", sig, "resolved_by", "fix", fix, True))
    graph.add_episode(ep, edges, max_episodes=int(cfg["max_episodes"]))
    return ep


def on_run_end(run: Any) -> None:
    """The Trace.end() hook. Off by config; a chat run with no tool calls and no task identity teaches nothing and is skipped."""
    try:
        cfg = mg_settings.load()
        if not cfg.get("enabled") or not getattr(run, "run_id", None):
            return
        from bot.agent_runtime import trace as tr

        store = getattr(run, "_store", None) or tr.get_store()
        build_episode(run.run_id, trace_store=store, task_id=getattr(run, "task_id", "") or "",
                      goal_hint=redact(getattr(run, "goal_hint", ""), 400), workspace=getattr(run, "workspace", None), cfg=cfg)
    except Exception:  # noqa: BLE001 - learning from a run must never change how it ends
        logger.debug("memory graph: could not build the episode", exc_info=True)

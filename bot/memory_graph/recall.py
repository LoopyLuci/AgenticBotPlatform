"""Fused retrieval: given a goal, which tools and skills have worked for this kind of task, which earlier episodes look like it,
and what to avoid.

Episode score = keyword (BM25 over goal + task type + tools + skills + file names)
              + graph proximity (the episode touched a file the goal names, or one codeintel hop from it)
              + success (the episode's outcome score, gated by relevance so an unrelated win does not surface)
              + recency (exponential decay, half-life from settings)
Tool / skill score blends the graph edge to the goal's task type (smoothed win rate, decayed, scaled by a confidence that grows
with n) with the outcomes of the relevant episodes that used it. A tool or skill that mostly shows up in failed relevant
episodes goes to `avoid`, with the errors those episodes ended on (and a known fix when the graph has one).

Phase hook: `semantic_scores()` is where embeddings plug in (P3); it returns {} now and its weight is 0.
"""
from __future__ import annotations

import logging
import math
import re
import time
from pathlib import Path
from typing import Any, Optional

from bot.memory_graph import settings as mg_settings
from bot.memory_graph import tasktype
from bot.memory_graph.store import Graph, decay, effective_weight

logger = logging.getLogger("bot.memory_graph")

_WORD = re.compile(r"[a-z0-9]+")
_FILE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,6}\b")
_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:_[A-Za-z0-9_]+|[a-z][A-Z][A-Za-z0-9]*)\b")
_STOP = frozenset("the and for with that this from into are was not you your can will should have has but all any use using".split())


def tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(str(text or "").lower()) if len(w) > 2 and w not in _STOP]


def bm25(query: list[str], docs: list[list[str]], k1: float = 1.4, b: float = 0.75) -> list[float]:
    n = len(docs)
    if not n or not query:
        return [0.0] * n
    avg = sum(len(d) for d in docs) / n or 1.0
    df: dict[str, int] = {}
    for d in docs:
        for w in set(d):
            df[w] = df.get(w, 0) + 1
    out = []
    for d in docs:
        tf: dict[str, int] = {}
        for w in d:
            tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for w in set(query):
            if w in tf:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(d) / avg))
        out.append(s)
    return out


def semantic_scores(goal: str, episodes: list[dict]) -> dict[str, float]:
    """PHASE 3 HOOK: {episode id: similarity in [0, 1]} from embeddings. Not wired yet."""
    return {}


def named_paths(goal: str, files: Optional[list[str]], root: Optional[Path]) -> set[str]:
    """Repo-relative paths the goal (or the caller) names."""
    out: set[str] = set()
    for raw in list(files or []) + _FILE.findall(goal or ""):
        p = str(raw).replace("\\", "/").strip("`'\" ")
        if root is not None:
            try:
                p = Path(p).resolve().relative_to(Path(root).resolve()).as_posix() if Path(p).is_absolute() else p
            except (ValueError, OSError):
                continue
        p = p.lstrip("./")
        if p:
            out.add(p)
    return out


def proximity(goal: str, files: Optional[list[str]], root: Optional[Path], cfg: dict) -> dict[str, float]:
    """{file: 1.0 named, 0.6 one codeintel hop away}. The hop uses codeintel's own tables at query time (imports both ways,
    files that reference a symbol the named file defines, the test map) - nothing is copied into the graph. {} without codeintel."""
    named = named_paths(goal, files, root)
    prox = {p: 1.0 for p in named}
    if root is None or not cfg.get("codeintel", True):
        return prox
    try:
        from bot.codeintel import store, tools as ci_tools

        ci_tools.ensure(Path(root))            # incremental, and skipped when the index was updated moments ago
        conn = store.connect(Path(root))
        try:
            known = {r["path"] for r in conn.execute("SELECT path FROM ci_files")}
            # a bare name in the goal ("auth.py") may be a suffix of an indexed path
            for p in list(named):
                if p not in known:
                    for hit in [k for k in known if k.endswith("/" + p)][:3]:
                        prox[hit] = 1.0
            seeds = {p for p, v in prox.items() if v == 1.0 and p in known}
            for ident in _IDENT.findall(goal or "")[:6]:       # a named symbol seeds its defining file
                for r in conn.execute("SELECT DISTINCT file FROM ci_symbols WHERE name=? LIMIT 3", (ident,)):
                    prox[r["file"]] = 1.0
                    seeds.add(r["file"])
            hop: set[str] = set()
            for f in seeds:
                hop |= {r["file"] for r in conn.execute("SELECT DISTINCT file FROM ci_imports WHERE resolved=?", (f,))}
                hop |= {r["resolved"] for r in conn.execute("SELECT DISTINCT resolved FROM ci_imports WHERE file=?", (f,))
                        if r["resolved"]}
                hop |= {r["test_file"] for r in conn.execute("SELECT DISTINCT test_file FROM ci_testmap WHERE source_file=?", (f,))}
                hop |= {r["file"] for r in conn.execute(
                    "SELECT DISTINCT r.file FROM ci_refs r JOIN ci_symbols s ON s.name = r.name WHERE s.file=? AND s.kind IN "
                    "('class','function') LIMIT 200", (f,))}
            for h in hop:
                if h and h not in prox:
                    prox[h] = 0.6
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - recall still works on keywords and edges
        logger.debug("memory graph: codeintel proximity failed", exc_info=True)
    return prox


def _doc(ep: dict) -> list[str]:
    return tokens(" ".join([ep.get("goal_summary", ""), ep.get("task_type", ""), " ".join(ep.get("tools") or {}),
                            " ".join(ep.get("skills") or []), " ".join(ep.get("files") or [])]))


def recall_for_task(goal: str, project: str = "", files: Optional[list[str]] = None, *, root: Any = None,
                    graph: Optional[Graph] = None, cfg: Optional[dict] = None, k: Optional[int] = None,
                    now: Optional[float] = None) -> dict:
    """{task_type, tools, skills, episodes, avoid}; every list is ranked best-first and capped at k."""
    cfg = cfg or mg_settings.load()
    k = int(k or cfg["recall_k"])
    now = now or time.time()
    half = float(cfg["half_life_days"])
    w = cfg["weights"]
    graph = graph or Graph()
    root_p = Path(str(root)) if root else None
    if root_p is not None and not root_p.is_dir():
        root_p = None
    task_type = tasktype.classify(goal)
    eps = graph.recent_episodes(int(cfg["scan_episodes"]), project) if project else []
    if not eps:
        eps = graph.recent_episodes(int(cfg["scan_episodes"]))
    prox = proximity(goal, files, root_p, cfg)
    kw = bm25(tokens(goal), [_doc(e) for e in eps])
    top = max(kw) if kw and max(kw) > 0 else 1.0
    sem = semantic_scores(goal, eps)
    scored = []
    for e, raw in zip(eps, kw):
        kws = raw / top if raw > 0 else 0.0
        near = max([prox.get(f, 0.0) for f in e.get("files") or []] or [0.0])
        rel = min(1.0, kws + near + sem.get(e["id"], 0.0))
        if e.get("task_type") == task_type:
            rel = min(1.0, rel + 0.05)
        if rel < 0.1:
            continue
        outcome = float((e.get("outcome") or {}).get("score", 0.5))
        score = (w["keyword"] * kws + w.get("graph", 0.0) * near + w["success"] * outcome * rel
                 + w["recency"] * decay(e["created"], half, now))
        scored.append((score, rel, kws, near, e))
    scored.sort(key=lambda r: r[0], reverse=True)

    def evidence(kind: str, names_of) -> list[dict]:
        edges = {r["src_key"]: r for r in graph.edges_to("task_type", task_type, "helped", kind)}
        used: dict[str, list[tuple[float, float]]] = {}
        for _s, rel, _k, _n, e in scored[:30]:
            for name in names_of(e):
                used.setdefault(name, []).append((rel, 2 * float((e.get("outcome") or {}).get("score", 0.5)) - 1))
        out = []
        for name in set(edges) | set(used):
            edge = edges.get(name)
            edge_term = 0.0
            if edge:
                conf = edge["n"] / (edge["n"] + float(cfg["confidence_n"]))
                edge_term = conf * effective_weight(edge, half, now) * decay(edge["last_seen"], half, now)
            obs = used.get(name, [])
            ep_signed = sum(r * s for r, s in obs) / (sum(r for r, _ in obs) + 0.5) if obs else 0.0
            score = 0.4 * edge_term + 0.6 * max(0.0, ep_signed)
            out.append({"name": name, "score": round(score, 4), "episode_signal": round(ep_signed, 3),
                        "n": edge["n"] if edge else len(obs), "wins": edge["wins"] if edge else sum(1 for _, s in obs if s > 0),
                        "fails": edge["fails"] if edge else sum(1 for _, s in obs if s < 0)})
        return out

    tool_ev = evidence("tool", lambda e: list(e.get("tools") or {}))
    skill_ev = evidence("skill", lambda e: list(e.get("skills") or []))
    avoid: list[dict] = []
    for kind, rows in (("tool", tool_ev), ("skill", skill_ev)):
        for r in rows:
            if r["episode_signal"] < -0.15 and r["fails"] >= r["wins"]:
                avoid.append({"kind": kind, "name": r["name"],
                              "reason": f"{r['fails']} failed / {r['wins']} ok on similar tasks"})
    bad = {a["name"] for a in avoid}
    seen: set[str] = set()
    for _s, _r, _k, _n, e in scored[:30]:
        if float((e.get("outcome") or {}).get("score", 1)) >= 0.6:
            continue
        for err in (e.get("errors") or [])[:2]:
            sig = err.get("sig")
            if sig and sig not in seen and len(seen) < 3:
                seen.add(sig)
                fix = graph.edges_from("error", sig, "resolved_by")
                avoid.append({"kind": "error", "name": sig, "reason": "ended a similar failed task"
                              + (f"; known fix: {fix[0]['dst_key']}" if fix else "")})

    def pick(rows: list[dict]) -> list[dict]:
        rows = [r for r in rows if r["name"] not in bad and r["score"] > 0 and r["name"] not in ("read_skill", "read_skill_file")]
        return sorted(rows, key=lambda r: (-r["score"], r["name"]))[:k]      # skill loads are reported as skills, not tools

    return {"task_type": task_type, "tools": pick(tool_ev), "skills": pick(skill_ev), "avoid": avoid[:k + 3],
            "episodes": [{"id": e["id"], "goal": e.get("goal_summary", ""), "outcome": (e.get("outcome") or {}).get("status"),
                          "score": round(s, 4), "tools": sorted(e.get("tools") or {}), "skills": e.get("skills") or [],
                          "files": (e.get("files") or [])[:5], "tokens": e.get("tokens")}
                         for s, _r, _k, _n, e in scored[:k]]}


def format_text(result: dict, max_chars: int = 4000) -> str:
    lines = [f"Task type: {result['task_type']}"]
    if result["tools"]:
        lines.append("Tools that worked on similar tasks: " + ", ".join(f"{t['name']} ({t['score']})" for t in result["tools"]))
    if result["skills"]:
        lines.append("Skills that worked: " + ", ".join(f"{t['name']} ({t['score']})" for t in result["skills"]))
    if result["avoid"]:
        lines.append("Avoid:")
        lines += [f"- {a['kind']} {a['name']}: {a['reason']}" for a in result["avoid"]]
    if result["episodes"]:
        lines.append("Similar earlier work:")
        lines += [f"- [{e['outcome']}] {e['goal'][:140]} (tools: {', '.join(e['tools'][:6])}; tokens: {e['tokens']})"
                  for e in result["episodes"]]
    if len(lines) == 1:
        lines.append("Nothing relevant in the memory graph yet.")
    return "\n".join(lines)[:max_chars]

"""The memory fabric's state: shared memories, recall by meaning, and model-independent conversation threads."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Callable, Optional

import numpy as np

from bot import db, memory

logger = logging.getLogger(__name__)

SHARED = 0                       # bot/memory.py's instance id for memories every bot and model shares
# Backends that keep the conversation themselves; the native loop's three share one history (agent_messages).
FAMILIES = {"api": "native", "custom_model": "native", "native_agent": "native"}
STATELESS = {"hermes_cli", "opencode", "openclaw"}          # one prompt in, one answer out: they remember nothing
HASH_FLOOR, RELATIVE = 0.06, 0.6     # recall: the hashed embedder's floor; the share of the best match a memory needs
DEFAULTS = {"shared_approval": True, "recall_k": 6, "recall_min": 0.45, "block_chars": 4000, "handoff_chars": 6000,
            "handoff_turns": 12, "inject": True, "auto_extract": True, "summarizer": "", "vault": True,
            "auto_fetch_minutes": 20}
_ready_for: Optional[str] = None      # the database file the tables were made in


def _conn():
    global _ready_for
    c = db.get_conn()
    if _ready_for != str(db.DB_PATH):
        with db._lock:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS mf_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mf_vectors (memory_id INTEGER NOT NULL, embedder TEXT NOT NULL, text_hash TEXT NOT NULL,
                vec BLOB NOT NULL, PRIMARY KEY (memory_id, embedder));
            CREATE TABLE IF NOT EXISTS mf_turns (id INTEGER PRIMARY KEY AUTOINCREMENT, thread TEXT NOT NULL,
                instance_id INTEGER, role TEXT NOT NULL, text TEXT NOT NULL, backend TEXT, model TEXT, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS mf_turns_thread ON mf_turns(thread, id);
            CREATE TABLE IF NOT EXISTS mf_summaries (thread TEXT PRIMARY KEY, upto INTEGER NOT NULL, summary TEXT NOT NULL,
                updated REAL NOT NULL);
            """)
            c.commit()
        _ready_for = str(db.DB_PATH)
    return c


def reset_cache() -> None:
    """A test (or a snapshot restore) pointed the database elsewhere."""
    global _ready_for
    _ready_for = None
    _embed_cache.clear()


# ---- settings ------------------------------------------------------------------------------------------------------ #

def settings() -> dict:
    rows = _conn().execute("SELECT key, value FROM mf_settings").fetchall()
    return {**DEFAULTS, **{k: json.loads(v) for k, v in rows}}


def set_settings(changes: dict) -> dict:
    bad = set(changes) - set(DEFAULTS)
    if bad:
        raise ValueError(f"unknown memory setting(s): {', '.join(sorted(bad))}")
    c = _conn()
    with db._lock:
        c.executemany("INSERT OR REPLACE INTO mf_settings(key, value) VALUES(?, ?)", [(k, json.dumps(v)) for k, v in changes.items()])
        c.commit()
    return settings()


# ---- memories ------------------------------------------------------------------------------------------------------ #

def remember(content: str, *, instance_id: Optional[int] = None, shared: bool = False, source: str = "user",
             kind: str = "fact") -> dict:
    """A memory for one bot, or (shared, or no bot) for every bot and model. bot/memory.py's gate and de-duplication
    apply; shared memories have their own gate (settings()["shared_approval"])."""
    target = SHARED if shared or instance_id is None else int(instance_id)
    out = memory.remember_full(target, content, source=source, kind=kind)
    return {**out, "shared": target == SHARED}


def approved(instance_id: Optional[int]) -> list[dict]:
    """Every approved memory this bot sees: the shared ones and its own."""
    rows = [{**dict(r), "shared": True} for r in db.list_memory_entries(SHARED, status="approved")]
    if instance_id not in (None, SHARED):
        rows += [{**dict(r), "shared": False} for r in db.list_memory_entries(int(instance_id), status="approved")]
    return rows


# ---- embeddings ---------------------------------------------------------------------------------------------------- #

_embed_cache: dict = {}


def _http_embed(url: str, model: str, texts: list[str]) -> Optional[np.ndarray]:
    import httpx
    try:
        r = httpx.post(f"{url.rstrip('/')}/api/embed", json={"model": model, "input": texts}, timeout=60)
        if r.status_code == 200:
            v = np.asarray(r.json()["embeddings"], dtype=np.float32)
            return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
    except Exception:  # noqa: BLE001 - another embedder is tried
        pass
    return None


def embedder() -> tuple[str, Callable[[list[str]], np.ndarray]]:
    """ABP_EMBED_URL, else ABP's own model server with an embedding model in its store, else a local Ollama's,
    else hashed TF-IDF (always there; matches shared words and word pieces, not meaning)."""
    hit = _embed_cache.get("e")
    if hit and time.time() - hit[2] < 300:
        return hit[0], hit[1]
    cands = []
    if os.environ.get("ABP_EMBED_URL"):
        cands.append((os.environ["ABP_EMBED_URL"], [os.environ.get("ABP_EMBED_MODEL", "nomic-embed-text")]))
    try:
        from bot.localai import engine, models
        if engine.installed():
            names = [m["name"] for m in models.listing() if any(k in m["name"].lower() for k in ("embed", "minilm", "bge", "e5-", "gte"))]
            cands.append((f"http://127.0.0.1:{engine.settings().get('port', 11436)}", names))
    except Exception:  # noqa: BLE001
        pass
    cands.append(("http://127.0.0.1:11434", ["nomic-embed-text", "nomic-embed-text:latest", "all-minilm"]))
    from bot import privacy
    if privacy.enabled():                                 # memories are never sent off this machine to be embedded
        cands = [c for c in cands if privacy.is_local_url(c[0])]
    choice = None
    for url, names in cands:
        for name in names[:3]:
            if _http_embed(url, name, ["probe"]) is not None:
                choice = (f"{name}@{url}", lambda t, u=url, m=name: _http_embed(u, m, t))
                break
        if choice:
            break
    if not choice:
        from bot.fileserver.index import hash_vectors
        choice = ("hash-tfidf", hash_vectors)
    _embed_cache["e"] = (choice[0], choice[1], time.time())
    return choice


def _vectors(rows: list[dict]) -> tuple[str, np.ndarray]:
    name, fn = embedder()
    c = _conn()
    have = {r[0]: (r[1], r[2]) for r in c.execute("SELECT memory_id, text_hash, vec FROM mf_vectors WHERE embedder=?", (name,))}
    out: list[Optional[np.ndarray]] = []
    todo = []
    for i, r in enumerate(rows):
        h = hashlib.sha1(r["content"].encode()).hexdigest()
        got = have.get(r["id"])
        if got and got[0] == h:
            out.append(np.frombuffer(got[1], dtype=np.float32))
        else:
            out.append(None)
            todo.append((i, h))
    if todo:
        vecs = fn([rows[i]["content"] for i, _ in todo])
        if vecs is None:                     # the embedder stopped answering: fall back for this call
            _embed_cache.clear()
            from bot.fileserver.index import hash_vectors
            return "hash-tfidf", hash_vectors([r["content"] for r in rows])
        with db._lock:
            for (i, h), v in zip(todo, vecs):
                out[i] = np.asarray(v, dtype=np.float32)
                c.execute("INSERT OR REPLACE INTO mf_vectors(memory_id, embedder, text_hash, vec) VALUES(?,?,?,?)",
                          (rows[i]["id"], name, h, out[i].tobytes()))
            c.commit()
    return name, np.vstack(out) if out else np.zeros((0, 1), dtype=np.float32)


def recall(query: str, instance_id: Optional[int] = None, k: Optional[int] = None) -> list[dict]:
    """The approved memories most related to `query`, best first, each with its similarity."""
    rows = approved(instance_id)
    if not rows or not query.strip():
        return []
    st = settings()
    name, mat = _vectors(rows)
    if name == "hash-tfidf":
        from bot.fileserver.index import hash_vectors as fn
    else:
        fn = embedder()[1]
    q = fn([query])
    if q is None:
        return []
    q = np.asarray(q, dtype=np.float32)[0]
    if q.shape[0] != mat.shape[1]:
        return []
    sims = mat @ q
    order = np.argsort(-sims)[: int(k or st["recall_k"])]
    # hashed TF-IDF scores sit far lower than a neural model's (a clear match is ~0.1-0.2), so each has its own floor;
    # and only memories close to the best match count, so a weak tail never rides along
    floor = HASH_FLOOR if name == "hash-tfidf" else float(st["recall_min"])
    best = float(sims[order[0]]) if len(order) else 0.0
    return [{**rows[i], "similarity": round(float(sims[i]), 3)} for i in order
            if sims[i] >= floor and sims[i] >= best * RELATIVE]


def memory_block(instance_id: Optional[int], query: str = "", budget_chars: Optional[int] = None) -> str:
    """The memories for a prompt: those related to what is being asked first, then the most confirmed, within a size
    budget; grouped by kind, shared and per-bot both. Empty when there are none."""
    rows = approved(instance_id)
    if not rows:
        return ""
    budget = int(budget_chars or settings()["block_chars"])
    related = recall(query, instance_id) if query else []
    seen = {(r["shared"], r["id"]) for r in related}
    rest = sorted((r for r in rows if (r["shared"], r["id"]) not in seen), key=memory._score, reverse=True)
    chosen, used = [], 0
    for r in related + rest:
        cost = len(r["content"]) + 4
        if used + cost > budget and chosen:
            break
        chosen.append(r)
        used += cost
    for r in related:
        if r in chosen:
            db.touch_memory_entry(r["id"])            # used: keeps it from fading
    lines = ["Long-term memory (shared by every model you run as; things you've been told to remember):"]
    for kind in memory.KINDS:
        group = [r for r in chosen if r.get("kind", "fact") == kind]
        if group:
            lines.append(f"{memory._KIND_TITLES[kind]}:")
            lines.extend(f"- {r['content']}" for r in group)
    if len(chosen) < len(rows):
        lines.append(f"({len(rows) - len(chosen)} more memories are not shown; memory_search finds them.)")
    return "\n".join(lines)


# ---- threads ------------------------------------------------------------------------------------------------------- #

def thread_key(instance_id: Optional[int], chat_id=None, thread_id=None) -> str:
    return f"{instance_id if instance_id is not None else '-'}:{chat_id if chat_id is not None else '-'}:{thread_id if thread_id is not None else '-'}"


def family(backend: str) -> str:
    return FAMILIES.get(backend, backend)


def record(thread: str, role: str, text: str, *, instance_id: Optional[int] = None, backend: str = "", model: str = "") -> int:
    c = _conn()
    with db._lock:
        cur = c.execute("INSERT INTO mf_turns(thread, instance_id, role, text, backend, model, created) VALUES(?,?,?,?,?,?,?)",
                        (thread, instance_id, role, text, backend or None, model or None, time.time()))
        c.commit()
        return cur.lastrowid


def turns(thread: str, after: int = 0, limit: int = 200) -> list[dict]:
    rows = _conn().execute("SELECT id, role, text, backend, model, created FROM mf_turns WHERE thread=? AND id>? ORDER BY id DESC LIMIT ?",
                           (thread, after, limit)).fetchall()
    return [dict(zip(("id", "role", "text", "backend", "model", "created"), r)) for r in reversed(rows)]


def threads(instance_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    q = "SELECT thread, COUNT(*), MAX(created), MAX(id) FROM mf_turns" + (" WHERE instance_id=?" if instance_id is not None else "")
    rows = _conn().execute(q + " GROUP BY thread ORDER BY MAX(id) DESC LIMIT ?",
                           ((instance_id, limit) if instance_id is not None else (limit,))).fetchall()
    return [{"thread": t, "turns": n, "last": last} for t, n, last, _ in rows]


def _last_seen(thread: str, backend: str) -> int:
    """The id of the last turn this backend's family answered in the thread (it holds everything up to there), or 0."""
    if backend in STATELESS:
        return 0
    fams = [b for b, f in FAMILIES.items() if f == family(backend)] or [backend]
    marks = ",".join("?" * len(fams))
    row = _conn().execute(f"SELECT MAX(id) FROM mf_turns WHERE thread=? AND role='assistant' AND backend IN ({marks})",
                          (thread, *fams)).fetchone()
    return int(row[0] or 0)


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def summary(thread: str, upto: int) -> str:
    """A running summary of the thread's turns up to (not including) `upto`: kept, and extended as turns age out."""
    c = _conn()
    row = c.execute("SELECT upto, summary FROM mf_summaries WHERE thread=?", (thread,)).fetchone()
    done, text = (int(row[0]), row[1]) if row else (0, "")
    new = [t for t in turns(thread, after=done, limit=10_000) if t["id"] < upto]
    if not new:
        return text
    lines = [text] if text else []
    for t in new:
        who = "The person" if t["role"] == "user" else f"The assistant ({t['model'] or t['backend'] or 'a model'})"
        lines.append(f"- {who}: {_clip(t['text'], 220 if t['role'] == 'user' else 300)}")
    text = "\n".join(lines)
    if len(text) > 4000:                                 # the oldest points give way to newer ones
        text = "- (earlier turns omitted)\n" + text[-3800:].split("\n", 1)[-1]
    with db._lock:
        c.execute("INSERT OR REPLACE INTO mf_summaries(thread, upto, summary, updated) VALUES(?,?,?,?)",
                  (thread, new[-1]["id"], text, time.time()))
        c.commit()
    return text


def handoff(thread: str, backend: str, model: str = "", budget_chars: Optional[int] = None) -> str:
    """What this backend has not seen of the conversation: the turns since its family last answered (all of them for
    a backend that keeps nothing), the most recent in full, older ones as a summary. Empty when it saw everything."""
    st = settings()
    budget = int(budget_chars or st["handoff_chars"])
    seen = _last_seen(thread, backend)
    missed = [t for t in turns(thread, after=seen, limit=500)]
    if missed and missed[-1]["role"] == "user":
        missed = missed[:-1]                               # the message being answered now is sent as the prompt
    if not missed:
        return ""
    recent: list[dict] = []
    used = 0
    for t in reversed(missed[-int(st["handoff_turns"]):]):
        cost = min(len(t["text"]), 1500) + 40
        if used + cost > budget * 0.75 and recent:
            break
        recent.insert(0, t)
        used += cost
    older_upto = recent[0]["id"] if recent else missed[-1]["id"] + 1
    lines = ["The conversation so far (it continues below; earlier turns may have been answered by another model):"]
    if missed[0]["id"] < older_upto:
        s = summary(thread, older_upto) if seen == 0 else "\n".join(
            f"- {'The person' if t['role'] == 'user' else 'The assistant'}: {_clip(t['text'], 200)}"
            for t in missed if t["id"] < older_upto)
        if s:
            lines += ["Earlier:", s[-int(budget * 0.25):]]
    for t in recent:
        who = "Person" if t["role"] == "user" else f"Assistant ({t['model'] or t['backend'] or 'model'})"
        lines.append(f"{who}: {_clip(t['text'], 1500)}")
    return "\n".join(lines)


def related_block(instance_id: Optional[int], prompt: str) -> str:
    """Only the memories related to this message (for a backend whose system prompt already holds the stable
    memory block: the system prompt stays the same turn to turn, so prompt caching keeps working)."""
    rel = recall(prompt, instance_id)
    if not rel:
        return ""
    for r in rel:
        db.touch_memory_entry(r["id"])
    return "Memories related to this message:\n" + "\n".join(f"- {r['content']}" for r in rel)


def context_block(instance_id: Optional[int], prompt: str, thread: str, backend: str, model: str = "") -> str:
    """What goes in front of the message for this backend: memories (all relevant ones for a backend that builds no
    system prompt of its own; only the related ones for ABP's native loop, whose system prompt has the rest) and
    the part of the conversation it missed."""
    if not settings()["inject"]:
        return ""
    mem = related_block(instance_id, prompt) if family(backend) == "native" else memory_block(instance_id, prompt)
    parts = [p for p in (mem, handoff(thread, backend, model)) if p]
    return "\n\n".join(parts)

"""The knowledge base under the memories: what ABP's sources say (folders, notes, GitHub repos, feeds, web pages,
conversations), kept as scored chunks, entities and summary trees that every model can query.

    ingest(source_id, item_id, title, text, ...)   canonicalise -> chunk (deterministic ids, bounded size) -> score
                                                   (signals + an admission gate) -> entities -> store -> embed ->
                                                   fold into the source's summary tree (L0 buffer seals into L1, L1s
                                                   into L2, ...)
    Retrieval (one dispatcher, query(mode, ...), every mode returning the same hit shape):
        search_entities  a name -> canonical entity ids          query_source   one source's nodes, by time and meaning
        drill_down       a summary's children                    cover_window   the fewest nodes covering a time span
        fetch_leaves     raw chunks by id                        walk           a question answered from the entity
                                                                                graph and the summaries, no model call
    Entities resolve to canonical ids (email:..., url:..., handle:..., tag:..., name:...); two entities on the same
    node are related (the co-occurrence graph is derived from the index, no separate store).

Summaries are extractive by default (deterministic, free); with a summariser model set (settings: "summarizer", a
model in ABP's local model server) they are written by that model instead.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from typing import Any, Optional

import numpy as np

from bot import db

CHUNK_TOKENS = 700            # a chunk's size bound (~4 characters a token)
SEAL_TOKENS = 3000            # an L0 buffer of this much becomes one L1 summary; LEVEL_FANOUT summaries seal one level up
LEVEL_FANOUT = 4
KEEP, DROP, THRESHOLD = 0.85, 0.15, 0.3          # the admission gate's bands (see score())
PRIORITY_BOOST = 0.25
KIND_WEIGHT = {"email": 1.0, "document": 0.8, "note": 0.9, "github": 0.85, "rss": 0.6, "web": 0.6, "conversation": 0.7,
               "chat": 0.5}
_ready_for: Optional[str] = None


def _conn():
    global _ready_for
    c = db.get_conn()
    if _ready_for != str(db.DB_PATH):
        with db._lock:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS kb_chunks (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, item_id TEXT NOT NULL, seq INTEGER NOT NULL,
                title TEXT, body TEXT NOT NULL, tokens INTEGER NOT NULL, ts REAL NOT NULL, kind TEXT, tags TEXT, score REAL,
                kept INTEGER NOT NULL, drop_reason TEXT, entities TEXT, sealed INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS kb_chunks_src ON kb_chunks(source_id, ts);
            CREATE INDEX IF NOT EXISTS kb_chunks_item ON kb_chunks(source_id, item_id);
            CREATE TABLE IF NOT EXISTS kb_entities (entity TEXT NOT NULL, kind TEXT NOT NULL, display TEXT NOT NULL,
                node_id TEXT NOT NULL, PRIMARY KEY (entity, node_id));
            CREATE INDEX IF NOT EXISTS kb_entities_node ON kb_entities(node_id);
            CREATE TABLE IF NOT EXISTS kb_summaries (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, level INTEGER NOT NULL,
                t0 REAL NOT NULL, t1 REAL NOT NULL, children TEXT NOT NULL, body TEXT NOT NULL, sealed INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS kb_summaries_src ON kb_summaries(source_id, level, t0);
            CREATE TABLE IF NOT EXISTS kb_vectors (node_id TEXT NOT NULL, embedder TEXT NOT NULL, vec BLOB NOT NULL,
                PRIMARY KEY (node_id, embedder));
            """)
            c.commit()
        _ready_for = str(db.DB_PATH)
    return c


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ---- canonicalise and chunk ---------------------------------------------------------------------------------------- #

def canonicalise(text: str) -> str:
    """Plain Markdown-ish text: HTML stripped, whitespace normalised, signatures and quoted replies dropped."""
    if re.search(r"<(p|div|br|html|body|span)\b", text, re.I):
        from html import unescape
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d)>", "\n", text)
        text = unescape(re.sub(r"<[^>]+>", " ", text))
    lines = []
    for line in text.replace("\r", "").split("\n"):
        if line.startswith(">"):                         # a quoted earlier message: it was ingested on its own
            continue
        if line.strip() in ("--", "-- "):               # a signature follows
            break
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def chunk(text: str, limit: int = CHUNK_TOKENS) -> list[str]:
    """Paragraph-respecting pieces of at most `limit` tokens (a paragraph longer than that is split by sentences)."""
    out, cur = [], ""
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    for p in paras:
        pieces = [p] if _tokens(p) <= limit else re.split(r"(?<=[.!?])\s+", p)
        for piece in pieces:
            while _tokens(piece) > limit:                    # one enormous sentence
                out.append(piece[: limit * 4])
                piece = piece[limit * 4:]
            if cur and _tokens(cur) + _tokens(piece) > limit:
                out.append(cur)
                cur = ""
            cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        out.append(cur)
    return out


# ---- entities ------------------------------------------------------------------------------------------------------ #

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_URL = re.compile(r"\bhttps?://[^\s<>()\]]+", re.I)
_HANDLE = re.compile(r"(?<![\w@])@([A-Za-z0-9_][A-Za-z0-9_.-]{1,38})\b|\b([A-Za-z0-9_.]{2,32})#(\d{4})\b")
_TAG = re.compile(r"(?<![\w#&])#([A-Za-z][\w-]{1,48})")
_NAME = re.compile(r"\b([A-Z][a-z]+(?:[ -](?:[A-Z][a-z]+|[A-Z]{2,}|van|de|von|der))+)\b")
_NOT_NAMES = {"The", "This", "That", "These", "Those", "When", "Then", "There", "What", "Where", "Which", "Monday", "Tuesday",
              "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December", "Thanks", "Hello", "Dear"}


def entities(text: str) -> list[tuple[str, str, str]]:
    """[(canonical id, kind, display)]: emails, URLs (by host and path), @handles, #tags and multi-word proper names."""
    found: dict[str, tuple[str, str]] = {}
    for m in _EMAIL.finditer(text):
        found[f"email:{m.group(0).lower()}"] = ("email", m.group(0))
    for m in _URL.finditer(text):
        u = m.group(0).rstrip(".,;:'\"")
        found[f"url:{re.sub(r'^https?://(www[.])?', '', u.lower()).rstrip('/')}"] = ("url", u)
    for m in _HANDLE.finditer(text):
        h = m.group(1) or f"{m.group(2)}#{m.group(3)}"
        if f"@{h}".lower() not in (e.split(":", 1)[1] for e in found if e.startswith("email:")):
            found[f"handle:{h.lower().rstrip('.')}"] = ("handle", "@" + h if m.group(1) else h)
    for m in _TAG.finditer(text):
        found[f"tag:{m.group(1).lower()}"] = ("tag", "#" + m.group(1))
    for m in _NAME.finditer(text):
        words = m.group(1).split()
        while words and words[0] in _NOT_NAMES:
            words = words[1:]
        if len(words) >= 2:
            name = " ".join(words)
            found[f"name:{name.lower()}"] = ("name", name)
    return [(k, v[0], v[1]) for k, v in found.items()]


# ---- scoring ------------------------------------------------------------------------------------------------------- #

def signals(body: str, kind: str, tags: list[str], ents: list) -> dict[str, float]:
    n = _tokens(body)
    words = re.findall(r"\w+", body.lower())
    size = 0.0 if n < 10 else min(1.0, (n - 10) / 20) if n < 30 else max(0.5, 1 - (n - 30) / 16000)
    ttr = 0.5 if len(words) < 20 else min(1.0, len(set(words)) / len(words) * 1.6)
    inter = 0.5 if not {"sent", "reply", "dm", "mention", "note", "authored"} & set(tags) else 1.0
    density = min(1.0, len(ents) / max(1.0, n / 100))
    return {"token_count": size, "unique_words": ttr, "source_weight": KIND_WEIGHT.get(kind, 0.6), "interaction": inter,
            "entity_density": density}


WEIGHTS = {"token_count": 1.0, "unique_words": 1.0, "source_weight": 1.5, "interaction": 3.0, "entity_density": 1.0}


def score(body: str, kind: str, tags: list[str], ents: list, importance=None) -> tuple[float, bool, str]:
    """(total, kept, reason). Cheap signals first: clear keeps and clear drops stop there; the borderline consults
    `importance` (a callable returning 0..1, e.g. a local model) when one is given. Priority-tagged chunks get a boost;
    tiny chunks with no entities are dropped whatever their metadata says."""
    sig = signals(body, kind, tags, ents)
    total = sum(sig[k] * w for k, w in WEIGHTS.items()) / sum(WEIGHTS.values())
    if "priority_high" in tags:
        total = min(1.0, total + PRIORITY_BOOST)
    elif _tokens(body) < 10 and not ents:
        return total, False, "tiny and without entities"
    if total >= KEEP:
        return total, True, ""
    if total <= DROP:
        return total, False, "low signal"
    if importance is not None:
        try:
            total = (total * sum(WEIGHTS.values()) + 2.0 * float(importance(body))) / (sum(WEIGHTS.values()) + 2.0)
        except Exception:  # noqa: BLE001 - the cheap total stands
            pass
    return total, total >= THRESHOLD, "" if total >= THRESHOLD else "below the threshold"


# ---- ingest -------------------------------------------------------------------------------------------------------- #

def _cid(source_id: str, item_id: str, seq: int) -> str:
    return "c_" + hashlib.sha1(f"{source_id}\0{item_id}\0{seq}".encode()).hexdigest()[:20]


def ingest(source_id: str, item_id: str, title: str, text: str, *, kind: str = "document", ts: Optional[float] = None,
           tags: Optional[list[str]] = None, importance=None) -> dict:
    """One item into the knowledge base. Re-ingesting the same (source, item) replaces its earlier chunks."""
    c = _conn()
    tags = list(tags or [])
    body = canonicalise(text)
    pieces = chunk(body) if body else []
    old = [r[0] for r in c.execute("SELECT id FROM kb_chunks WHERE source_id=? AND item_id=?", (source_id, item_id))]
    kept = dropped = 0
    rows = []
    for i, p in enumerate(pieces):
        ents = entities(f"{title}\n{p}" if i == 0 else p)
        total, ok, why = score(p, kind, tags, ents, importance)
        rows.append((_cid(source_id, item_id, i), source_id, item_id, i, title, p, _tokens(p), float(ts or time.time()), kind,
                     json.dumps(tags), round(total, 4), int(ok), why or None, json.dumps([e[0] for e in ents]), 0, time.time(), ents))
        kept += ok
        dropped += not ok
    with db._lock:
        if old:
            marks = ",".join("?" * len(old))
            c.execute(f"DELETE FROM kb_chunks WHERE id IN ({marks})", old)
            c.execute(f"DELETE FROM kb_entities WHERE node_id IN ({marks})", old)
            c.execute(f"DELETE FROM kb_vectors WHERE node_id IN ({marks})", old)
        for r in rows:
            c.execute("INSERT OR REPLACE INTO kb_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r[:16])
            if r[11]:
                c.executemany("INSERT OR IGNORE INTO kb_entities(entity, kind, display, node_id) VALUES (?,?,?,?)",
                              [(e, k, d, r[0]) for e, k, d in r[16]])
        c.commit()
    if old:
        _unseal(source_id)                       # an item changed: its summaries are rebuilt from the chunks
    _embed([(r[0], r[5]) for r in rows if r[11]])
    seal(source_id)
    return {"source_id": source_id, "item_id": item_id, "chunks": len(rows), "kept": kept, "dropped": dropped}


def remove_item(source_id: str, item_id: str) -> int:
    c = _conn()
    ids = [r[0] for r in c.execute("SELECT id FROM kb_chunks WHERE source_id=? AND item_id=?", (source_id, item_id))]
    if ids:
        marks = ",".join("?" * len(ids))
        with db._lock:
            for t in ("kb_chunks WHERE id", "kb_entities WHERE node_id", "kb_vectors WHERE node_id"):
                c.execute(f"DELETE FROM {t} IN ({marks})", ids)
            c.commit()
        _unseal(source_id)
        seal(source_id)
    return len(ids)


def remove_source(source_id: str) -> None:
    c = _conn()
    ids = [r[0] for r in c.execute("SELECT id FROM kb_chunks WHERE source_id=? UNION SELECT id FROM kb_summaries WHERE source_id=?",
                                   (source_id, source_id))]
    with db._lock:
        c.execute("DELETE FROM kb_chunks WHERE source_id=?", (source_id,))
        c.execute("DELETE FROM kb_summaries WHERE source_id=?", (source_id,))
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            c.execute(f"DELETE FROM kb_entities WHERE node_id IN ({marks})", part)
            c.execute(f"DELETE FROM kb_vectors WHERE node_id IN ({marks})", part)
        c.commit()


# ---- embeddings ---------------------------------------------------------------------------------------------------- #

def _embed(items: list[tuple[str, str]]) -> None:
    if not items:
        return
    from bot.memoryfabric.store import embedder
    name, fn = embedder()
    vecs = fn([t for _, t in items])
    if vecs is None:
        return
    c = _conn()
    with db._lock:
        c.executemany("INSERT OR REPLACE INTO kb_vectors(node_id, embedder, vec) VALUES (?,?,?)",
                      [(i, name, np.asarray(v, dtype=np.float32).tobytes()) for (i, _), v in zip(items, vecs)])
        c.commit()


def _rank(nodes: list[dict], query: str) -> list[dict]:
    """Nodes reordered by meaning against `query` (embedding what is missing first)."""
    if not nodes or not query.strip():
        return nodes
    from bot.memoryfabric.store import embedder
    name, fn = embedder()
    c = _conn()
    have = {r[0]: np.frombuffer(r[1], dtype=np.float32) for r in c.execute(
        f"SELECT node_id, vec FROM kb_vectors WHERE embedder=? AND node_id IN ({','.join('?' * len(nodes))})",
        (name, *[n["node_id"] for n in nodes]))}
    missing = [(n["node_id"], n["content"]) for n in nodes if n["node_id"] not in have]
    if missing:
        _embed(missing)
        have.update({r[0]: np.frombuffer(r[1], dtype=np.float32) for r in c.execute(
            f"SELECT node_id, vec FROM kb_vectors WHERE embedder=? AND node_id IN ({','.join('?' * len(missing))})",
            (name, *[m[0] for m in missing]))})
    q = fn([query])
    if q is None:
        return nodes
    q = np.asarray(q, dtype=np.float32)[0]
    for n in nodes:
        v = have.get(n["node_id"])
        n["score"] = round(float(v @ q), 4) if v is not None and v.shape == q.shape else 0.0
    return sorted(nodes, key=lambda n: -n["score"])


# ---- summary trees ------------------------------------------------------------------------------------------------- #

def _summarise(texts: list[str], limit_chars: int = 1600) -> str:
    """Extractive: the sentences carrying the most of the texts' frequent words and entities, in their original order;
    a local model writes it instead when one is set (settings()["summarizer"])."""
    from bot.memoryfabric.store import settings
    model = settings().get("summarizer") or ""
    joined = "\n\n".join(texts)
    if model:
        try:
            import httpx

            from bot.localai import engine
            port = engine.settings().get("port", 11436)
            r = httpx.post(f"http://127.0.0.1:{port}/api/chat", timeout=300, json={
                "model": model, "stream": False, "options": {"temperature": 0, "num_predict": 400},
                "messages": [{"role": "system", "content": "Summarise for a personal knowledge base: facts, names, dates, "
                                                            "decisions. No preamble. At most 8 bullet points."},
                             {"role": "user", "content": joined[:24000]}]})
            text = r.json()["message"]["content"].strip()
            if text:
                return text[:limit_chars]
        except Exception:  # noqa: BLE001 - the extractive summary stands in
            pass
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", joined) if len(s.strip()) > 20]
    if not sents:
        return joined[:limit_chars]
    freq = Counter(w for s in sents for w in set(re.findall(r"[a-z]{4,}", s.lower())))
    ents = {e[0] for s in sents for e in entities(s)}
    scored = []
    for i, s in enumerate(sents):
        ws = set(re.findall(r"[a-z]{4,}", s.lower()))
        val = sum(math.log(1 + freq[w]) for w in ws) / (1 + len(ws)) ** 0.5 + 0.8 * sum(1 for e in entities(s) if e[0] in ents)
        scored.append((val, i, s))
    chosen, used = [], 0
    for _val, i, s in sorted(scored, reverse=True):
        if used + len(s) > limit_chars:
            continue
        chosen.append((i, s))
        used += len(s) + 3
    return "\n".join(f"- {s}" for _, s in sorted(chosen))


def _sid(source_id: str, level: int, children: list[str]) -> str:
    return f"s{level}_" + hashlib.sha1(f"{source_id}\0{level}\0{','.join(children)}".encode()).hexdigest()[:18]


def _unseal(source_id: str) -> None:
    """Drop the source's summaries; seal() rebuilds them from the chunks (deterministic, so ids come out the same)."""
    c = _conn()
    ids = [r[0] for r in c.execute("SELECT id FROM kb_summaries WHERE source_id=?", (source_id,))]
    with db._lock:
        c.execute("DELETE FROM kb_summaries WHERE source_id=?", (source_id,))
        c.execute("UPDATE kb_chunks SET sealed=0 WHERE source_id=?", (source_id,))
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            c.execute(f"DELETE FROM kb_entities WHERE node_id IN ({','.join('?' * len(part))})", part)
        c.commit()


def seal(source_id: str, force: bool = False) -> int:
    """Fold the source's unsealed kept chunks (oldest first) into L1 summaries of ~SEAL_TOKENS, and every LEVEL_FANOUT
    unsealed summaries of a level into one of the next. force: seal what is left even if short (a daily close)."""
    c = _conn()
    made = 0
    level = 0
    while True:
        if level == 0:
            rows = c.execute("SELECT id, body, tokens, ts FROM kb_chunks WHERE source_id=? AND kept=1 AND sealed=0 ORDER BY ts, id",
                             (source_id,)).fetchall()
            groups, cur, size = [], [], 0
            for r in rows:
                cur.append(r)
                size += r[2]
                if size >= SEAL_TOKENS:
                    groups.append(cur)
                    cur, size = [], 0
            if cur and force:
                groups.append(cur)
            items = [[(r[0], r[1], r[3], r[3]) for r in g] for g in groups]
        else:
            rows = c.execute("SELECT id, body, t0, t1 FROM kb_summaries WHERE source_id=? AND level=? AND sealed=0 ORDER BY t0, id",
                             (source_id, level)).fetchall()
            n = len(rows) if force and len(rows) > 1 else len(rows) // LEVEL_FANOUT * LEVEL_FANOUT
            items = [list(rows[i:i + LEVEL_FANOUT]) for i in range(0, n, LEVEL_FANOUT)] if not force else \
                ([list(rows)] if len(rows) > 1 else [])
        if not items:
            if level > 0 or not c.execute("SELECT 1 FROM kb_summaries WHERE source_id=? AND level=1 AND sealed=0", (source_id,)).fetchone():
                break
            level += 1
            continue
        for g in items:
            children = [x[0] for x in g]
            sid = _sid(source_id, level + 1, children)
            body = _summarise([x[1] for x in g])
            ents = {(e, k, d) for e, k, d in entities(body)}
            child_marks = ",".join("?" * len(children))
            ents |= {tuple(r) for r in c.execute(f"SELECT entity, kind, display FROM kb_entities WHERE node_id IN ({child_marks})", children)}
            with db._lock:
                c.execute("INSERT OR REPLACE INTO kb_summaries VALUES (?,?,?,?,?,?,?,?,?)",
                          (sid, source_id, level + 1, min(x[2] for x in g), max(x[3] for x in g), json.dumps(children), body, 0, time.time()))
                c.executemany("INSERT OR IGNORE INTO kb_entities(entity, kind, display, node_id) VALUES (?,?,?,?)",
                              [(e, k, d, sid) for e, k, d in ents])
                table = "kb_chunks" if level == 0 else "kb_summaries"
                c.execute(f"UPDATE {table} SET sealed=1 WHERE id IN ({child_marks})", children)
                c.commit()
            _embed([(sid, body)])
            made += 1
        level += 1
        if level > 8:
            break
    return made


# ---- retrieval ----------------------------------------------------------------------------------------------------- #

def _iso(t: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _ents(node_id: str) -> list[str]:
    return [r[0] for r in _conn().execute("SELECT entity FROM kb_entities WHERE node_id=? ORDER BY entity", (node_id,))]


def _hit_chunk(r) -> dict:
    return {"node_id": r[0], "node_kind": "leaf", "source_id": r[1], "level": 0, "content": r[5], "title": r[4],
            "time_range_start": _iso(r[7]), "time_range_end": _iso(r[7]), "entities": _ents(r[0]), "child_ids": [],
            "source_ref": {"source_id": r[1], "item_id": r[2], "seq": r[3]}, "score": 0.0, "_t0": r[7], "_t1": r[7]}


def _hit_summary(r) -> dict:
    return {"node_id": r[0], "node_kind": "summary", "source_id": r[1], "level": r[2], "content": r[6],
            "time_range_start": _iso(r[3]), "time_range_end": _iso(r[4]), "entities": _ents(r[0]),
            "child_ids": json.loads(r[5]), "score": 0.0, "_t0": r[3], "_t1": r[4]}


def _node(node_id: str) -> Optional[dict]:
    c = _conn()
    r = c.execute("SELECT * FROM kb_chunks WHERE id=?", (node_id,)).fetchone()
    if r:
        return _hit_chunk(r)
    r = c.execute("SELECT * FROM kb_summaries WHERE id=?", (node_id,)).fetchone()
    return _hit_summary(r) if r else None


def _clean(hits: list[dict], limit: int) -> dict:
    total = len(hits)
    hits = hits[:limit]
    for h in hits:
        h.pop("_t0", None)
        h.pop("_t1", None)
    return {"hits": hits, "total": total, "truncated": total > len(hits)}


def search_entities(name: str, limit: int = 20) -> list[dict]:
    """A surface form ("alice", "@alice", "Alice Smith") -> canonical entities that match, most mentioned first."""
    q = f"%{name.strip().lstrip('@#').lower()}%"
    rows = _conn().execute("SELECT entity, kind, MIN(display), COUNT(*) FROM kb_entities WHERE lower(entity) LIKE ? OR lower(display) LIKE ? "
                           "GROUP BY entity, kind ORDER BY COUNT(*) DESC LIMIT ?", (q, q, limit)).fetchall()
    return [{"entity": e, "kind": k, "display": d, "mentions": n} for e, k, d, n in rows]


def neighbors(entity: str, limit: int = 20) -> list[dict]:
    """Entities that appear on the same nodes as `entity`, by how many nodes they share (the co-occurrence graph)."""
    rows = _conn().execute("SELECT b.entity, COUNT(DISTINCT b.node_id) FROM kb_entities a JOIN kb_entities b ON a.node_id=b.node_id "
                           "WHERE a.entity=? AND b.entity<>? GROUP BY b.entity ORDER BY 2 DESC LIMIT ?", (entity, entity, limit)).fetchall()
    return [{"subject": entity, "object": o, "weight": w} for o, w in rows]


def query_source(source_id: str, query: str = "", since: Optional[float] = None, until: Optional[float] = None,
                 limit: int = 10) -> dict:
    c = _conn()
    lo, hi = since or 0.0, until or 1e13
    hits = [_hit_summary(r) for r in c.execute("SELECT * FROM kb_summaries WHERE source_id=? AND t1>=? AND t0<=? ORDER BY level DESC, t0",
                                               (source_id, lo, hi))]
    hits += [_hit_chunk(r) for r in c.execute("SELECT * FROM kb_chunks WHERE source_id=? AND kept=1 AND sealed=0 AND ts BETWEEN ? AND ? ORDER BY ts",
                                              (source_id, lo, hi))]
    return _clean(_rank(hits, query) if query else hits, limit)


def drill_down(node_id: str, depth: int = 1, query: str = "", limit: int = 20) -> dict:
    frontier, out = [node_id], []
    for _ in range(max(1, depth)):
        nxt = []
        for nid in frontier:
            n = _node(nid)
            for ch in (n or {}).get("child_ids", []):
                h = _node(ch)
                if h:
                    out.append(h)
                    nxt.append(ch)
        frontier = nxt
    return _clean(_rank(out, query) if query else out, limit)


def cover_window(since: float, until: float, source_id: str = "", limit: int = 20) -> dict:
    """The fewest nodes covering [since, until]: the highest summaries wholly inside it, then whatever is left of
    the span from lower levels and raw chunks."""
    c = _conn()
    where, args = ("AND source_id=?", (source_id,)) if source_id else ("", ())
    sums = [_hit_summary(r) for r in c.execute(f"SELECT * FROM kb_summaries WHERE t0>=? AND t1<=? {where} ORDER BY level DESC, t0",
                                               (since, until, *args))]
    chosen, covered = [], set()
    for s in sums:
        kids = _leaves_of(s["node_id"])
        if kids & covered:
            continue
        chosen.append(s)
        covered |= kids
    for r in c.execute(f"SELECT * FROM kb_chunks WHERE kept=1 AND ts BETWEEN ? AND ? {where} ORDER BY ts", (since, until, *args)):
        if r[0] not in covered:
            chosen.append(_hit_chunk(r))
    return _clean(sorted(chosen, key=lambda h: h["_t0"]), limit)


def _leaves_of(node_id: str) -> set[str]:
    n = _node(node_id)
    if not n:
        return set()
    if n["node_kind"] == "leaf":
        return {node_id}
    out: set[str] = set()
    for ch in n["child_ids"]:
        out |= _leaves_of(ch)
    return out


def fetch_leaves(ids: list[str]) -> dict:
    hits = [h for h in (_node(i) for i in ids[:20]) if h and h["node_kind"] == "leaf"]
    return _clean(hits, 20)


def walk(question: str, limit: int = 10, max_hops: int = 2, time_window_days: Optional[float] = None) -> dict:
    """A question answered from the index without a model call: the question's entities, when related to each other
    through the co-occurrence graph, narrow the nodes to those carrying them (ranked by how many they carry, then how
    recent); otherwise the summaries and chunks are ranked by meaning (and by how many question entities they name)."""
    c = _conn()
    q_ents = [e[0] for e in entities(question)]
    q_ents += [r["entity"] for w in re.findall(r"[A-Za-z][\w.-]{3,}", question) for r in search_entities(w, 3)
               if r["kind"] in ("name", "handle", "tag", "email") and w.lower() in r["display"].lower()]
    q_ents = list(dict.fromkeys(q_ents))
    since = time.time() - time_window_days * 86400 if time_window_days else 0.0
    if len(q_ents) >= 1:
        related = [(a, b) for i, a in enumerate(q_ents) for b in q_ents[i + 1:] if _hops(a, b, max_hops) is not None]
        groups = related or ([(q_ents[0], q_ents[0])] if len(q_ents) == 1 else [])
        if groups:
            nodes: Counter = Counter()
            for a, b in groups:
                na = {r[0] for r in c.execute("SELECT node_id FROM kb_entities WHERE entity=?", (a,))}
                nb = {r[0] for r in c.execute("SELECT node_id FROM kb_entities WHERE entity=?", (b,))}
                for n in na & nb:
                    nodes[n] += 2 if a != b else 1
            hits = [h for h in (_node(n) for n in nodes) if h and h["_t1"] >= since]
            for h in hits:
                h["score"] = float(nodes[h["node_id"]] + sum(e in h["entities"] for e in q_ents))
            hits.sort(key=lambda h: (-h["score"], -h["_t1"]))
            if hits:
                return {"route": "local", "query_entities": q_ents, **_clean(hits, limit)}
    cands = [_hit_summary(r) for r in c.execute("SELECT * FROM kb_summaries WHERE t1>=?", (since,))]
    cands += [_hit_chunk(r) for r in c.execute("SELECT * FROM kb_chunks WHERE kept=1 AND sealed=0 AND ts>=?", (since,))]
    ranked = _rank(cands, question)[: max(limit * 2, 20)]
    if q_ents:
        ranked.sort(key=lambda h: (-sum(e in h["entities"] for e in q_ents), -h["score"]))
    return {"route": "global", "query_entities": q_ents, **_clean(ranked, limit)}


def _hops(a: str, b: str, max_hops: int) -> Optional[int]:
    if a == b:
        return 0
    seen, frontier = {a}, {a}
    for d in range(1, max_hops + 1):
        nxt = set()
        for x in frontier:
            nxt |= {n["object"] for n in neighbors(x, 200)}
        if b in nxt:
            return d
        frontier = nxt - seen
        seen |= nxt
        if not frontier:
            return None
    return None


def query(mode: str, **kw) -> Any:
    """The one entry point the agents' memory_tree tool, the API and MCP use."""
    if mode == "search_entities":
        return {"entities": search_entities(kw["name"], int(kw.get("limit", 20)))}
    if mode == "neighbors":
        return {"edges": neighbors(kw["entity"], int(kw.get("limit", 20)))}
    if mode == "query_source":
        return query_source(kw["source_id"], kw.get("query", ""), kw.get("since"), kw.get("until"), int(kw.get("limit", 10)))
    if mode == "drill_down":
        return drill_down(kw["node_id"], int(kw.get("depth", 1)), kw.get("query", ""), int(kw.get("limit", 20)))
    if mode == "cover_window":
        return cover_window(float(kw["since"]), float(kw["until"]), kw.get("source_id", ""), int(kw.get("limit", 20)))
    if mode == "fetch_leaves":
        return fetch_leaves(list(kw["ids"]))
    if mode == "walk":
        return walk(kw["query"], int(kw.get("limit", 10)), int(kw.get("max_hops", 2)), kw.get("time_window_days"))
    if mode == "ingest_document":
        return ingest(kw.get("source_id") or "documents", kw.get("item_id") or hashlib.sha1(kw["text"].encode()).hexdigest()[:16],
                      kw.get("title", ""), kw["text"], kind="document", tags=["authored"])
    raise ValueError("mode is search_entities, neighbors, query_source, drill_down, cover_window, fetch_leaves, walk or ingest_document")


def stats() -> dict:
    c = _conn()
    per = {s: {"chunks": n, "kept": k} for s, n, k in c.execute("SELECT source_id, COUNT(*), SUM(kept) FROM kb_chunks GROUP BY source_id")}
    for s, n, top in c.execute("SELECT source_id, COUNT(*), MAX(level) FROM kb_summaries GROUP BY source_id"):
        per.setdefault(s, {"chunks": 0, "kept": 0}).update(summaries=n, levels=top)
    for s, last in c.execute("SELECT source_id, MAX(ts) FROM kb_chunks GROUP BY source_id"):
        per[s]["last"] = last
    return {"sources": per, "entities": c.execute("SELECT COUNT(DISTINCT entity) FROM kb_entities").fetchone()[0]}

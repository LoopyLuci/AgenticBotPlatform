"""The vault: a small SQLite index (data dir) plus item bodies in an ordered list of tiers.

Placement. A new item goes to the first durable tier. Reading an item bumps its hits and last_access; when its
keep score is high enough it is (a) copied into the RAM tier (a cache: the durable copy stays) and (b) moved one
durable tier up (background). A tier over its max_bytes sheds the items with the lowest keep score to the next
durable tier (cascading); the last tier, when over its own cap, deletes the lowest-importance oldest items (logged).

A move is crash-safe: mark the row `moving` -> write the destination -> verify the hash -> update the index ->
remove the source. `recover()` (run when the vault opens) finishes or rolls back whatever a crash left half done.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from bot.context_vault import search, settings as vsettings, tiers as vtiers
from bot.context_vault.redact import sanitize

logger = logging.getLogger("bot.context_vault")

KINDS = ("decision", "plan", "file_summary", "error_fix", "preference", "rule", "task_state", "brief", "review", "note")
BASE_IMPORTANCE = {"rule": 0.8, "preference": 0.7, "decision": 0.7, "error_fix": 0.6, "brief": 0.55, "plan": 0.5,
                   "task_state": 0.5, "review": 0.5, "file_summary": 0.45, "note": 0.3}
COUNTERS = ("captured", "deduped", "skipped", "promoted", "demoted", "evicted", "cache_evictions", "lost", "dropped",
            "recalls", "recalls_with_results", "reads_hot", "reads_cold", "recovered")
PREVIEW = 400


def _now() -> float:
    return time.time()


def item_id(text: str) -> str:
    norm = " ".join(text.lower().split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]


def est_tokens(text: str) -> int:
    return len(text) // 4 + 1


class Vault:
    def __init__(self, cfg: Optional[dict] = None, index_file: Optional[Path] = None, *,
                 embedder: Optional[Callable] = None, clock: Callable[[], float] = _now,
                 backends: Optional[list] = None, recover: bool = True):
        self.cfg = cfg if cfg is not None else vsettings.load()
        self.index_file = Path(index_file) if index_file else vsettings.index_path()
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.embedder = embedder
        self._lock = threading.RLock()
        self._move_lock = threading.RLock()
        self._tasks: "queue.Queue[Callable[[], Any]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_maint = 0.0
        self._fault: Optional[Callable[[str], None]] = None          # tests: called at each stage of a move
        self._db = sqlite3.connect(str(self.index_file), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._init_db()
        self.tiers: list[vtiers.TierBackend] = backends if backends is not None else self._build_tiers()
        self.caps = {t.name: self._cap_of(t.name) for t in self.tiers}
        self.durable = [t for t in self.tiers if not t.volatile]
        self.cache = next((t for t in self.tiers if t.volatile), None)
        if not self.durable:
            raise ValueError("the context vault needs at least one non-RAM tier")
        if recover:
            self.recover()

    # ------------------------------------------------------------------ setup
    def _build_tiers(self) -> list[vtiers.TierBackend]:
        specs = [dict(s) for s in (self.cfg.get("tiers") or []) if isinstance(s, dict)]
        base = self.index_file.parent / "store"
        out: list[vtiers.TierBackend] = []
        seen = set()
        dirs = sum(1 for s in specs if str(s.get("kind") or "dir") == "dir")
        for i, spec in enumerate(specs):
            spec.setdefault("name", f"tier{i}")
            if spec["name"] in seen:
                raise ValueError(f"two tiers are named {spec['name']!r}")
            seen.add(spec["name"])
            default = base if dirs <= 1 else base / spec["name"]
            out.append(vtiers.make_backend(spec, default))
        if not any(not t.volatile for t in out):
            out.append(vtiers.DirBackend("store", base))
        return out

    def _cap_of(self, name: str) -> int:
        for s in self.cfg.get("tiers") or []:
            if isinstance(s, dict) and s.get("name") == name:
                try:
                    return max(0, int(s.get("max_bytes") or 0))
                except (TypeError, ValueError):
                    return 0
        return 0

    def _init_db(self) -> None:
        with self._lock:
            self._db.executescript("""
            CREATE TABLE IF NOT EXISTS items(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT DEFAULT '', project TEXT DEFAULT '',
              paths TEXT DEFAULT '[]', tags TEXT DEFAULT '[]', created REAL, last_access REAL, hits INTEGER DEFAULT 0,
              importance REAL DEFAULT 0.5, size INTEGER DEFAULT 0, sha TEXT DEFAULT '', preview TEXT DEFAULT '',
              embedding BLOB, tier TEXT NOT NULL, hot TEXT, state TEXT DEFAULT 'ok', move_to TEXT, moved_at REAL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS items_tier ON items(tier, state);
            CREATE INDEX IF NOT EXISTS items_project ON items(project, kind);
            CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER DEFAULT 0);
            """)
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._db.commit()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self._db.execute(sql, args)
            self._db.commit()

    def _incr(self, name: str, n: int = 1) -> None:
        self._x("INSERT INTO counters(name, value) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET value = value + ?",
                (name, n, n))

    def _backend(self, name: Optional[str]) -> Optional[vtiers.TierBackend]:
        return next((t for t in self.tiers if t.name == name), None)

    def close(self) -> None:
        self.stop()
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------ scoring
    def keep_score(self, row, now: Optional[float] = None) -> float:
        """0..1: how much an item deserves a fast tier (importance, frequency, recency)."""
        now = self.clock() if now is None else now
        age_days = max(0.0, (now - float(row["last_access"] or row["created"] or now)) / 86400.0)
        recency = math.exp(-age_days / 7.0)
        freq = min(1.0, math.log2(1 + int(row["hits"] or 0)) / 4.0)
        return 0.3 * float(row["importance"] or 0) + 0.4 * freq + 0.3 * recency

    # ------------------------------------------------------------------ writing
    def add(self, text: str, kind: str = "note", *, source: str = "", project: str = "", paths=(), tags=(),
            importance: Optional[float] = None, embedding=None, created: Optional[float] = None) -> dict:
        """Store one item. -> {"status": stored|deduped|skipped, "id"?}. Never raises for bad input."""
        kind = kind if kind in KINDS else "note"
        clean = sanitize(text if isinstance(text, str) else "")
        capcfg = self.cfg.get("capture") or {}
        max_chars = int(capcfg.get("max_chars") or 4000)
        min_chars = int(capcfg.get("min_chars") or 0)
        if clean is None:
            self._incr("skipped")
            return {"status": "skipped", "reason": "credential-like or empty"}
        clean = clean.strip()[:max(max_chars, 200)]
        if len(clean) < min_chars:
            self._incr("skipped")
            return {"status": "skipped", "reason": "too short"}
        iid = item_id(clean)
        now = self.clock()
        base = BASE_IMPORTANCE.get(kind, 0.4) if importance is None else max(0.0, min(1.0, float(importance)))
        with self._lock:
            row = self._db.execute("SELECT id, importance FROM items WHERE id = ?", (iid,)).fetchone()
            if row is not None:
                self._db.execute("UPDATE items SET importance = ?, last_access = ? WHERE id = ?",
                                 (min(1.0, max(row["importance"], base) + 0.02), now, iid))
                self._db.commit()
        if row is not None:
            self._incr("deduped")
            return {"status": "deduped", "id": iid}
        vec = list(embedding) if embedding is not None else self._embed_one(clean)
        near = self._near_duplicate(clean, kind, project, vec)
        if near:
            self._x("UPDATE items SET last_access = ?, importance = MIN(1.0, importance + 0.02) WHERE id = ?", (now, near))
            self._incr("deduped")
            return {"status": "deduped", "id": near, "near": True}
        data = clean.encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        top = self.durable[0]
        top.put(iid, data)                                    # body first: a crash here leaves an orphan, swept at start
        paths_l, tags_l = [str(p) for p in paths][:20], [str(t) for t in tags][:20]
        self._x("INSERT INTO items(id, kind, source, project, paths, tags, created, last_access, hits, importance, size, "
                "sha, preview, embedding, tier) VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)",
                (iid, kind, source[:200], project[:200], json.dumps(paths_l), json.dumps(tags_l), created or now, now, base,
                 len(data), sha, clean[:PREVIEW], search.pack(vec), top.name))
        self._incr("captured")
        self.submit(self.rebalance)
        return {"status": "stored", "id": iid, "tier": top.name}

    def _embed_one(self, text: str) -> Optional[list[float]]:
        if self.embedder is None:
            return None
        try:
            out = self.embedder([text[:2000]])
            return list(out[0]) if out else None
        except Exception as exc:  # noqa: BLE001 - optional
            logger.debug("context vault: embedder failed: %s", exc)
            return None

    def _near_duplicate(self, text: str, kind: str, project: str, vec: Optional[list[float]]) -> Optional[str]:
        thr = float(self.cfg.get("dedup_threshold") or 0.92)
        rows = self._q("SELECT id, preview, embedding FROM items WHERE kind = ? AND project = ? "
                       "ORDER BY created DESC LIMIT 1000", (kind, project))
        for r in rows:
            other = search.unpack(r["embedding"])
            if vec is not None and other is not None:
                if search.cosine(vec, other) >= thr:
                    return r["id"]
            elif search.jaccard(text[:PREVIEW], r["preview"]) >= 0.9:
                return r["id"]
        return None

    # ------------------------------------------------------------------ reading
    def get(self, iid: str, *, touch: bool = True) -> Optional[dict]:
        rows = self._q("SELECT * FROM items WHERE id = ?", (iid,))
        if not rows:
            return None
        row = rows[0]
        data = self._read_best(row)
        if data is None:
            return None
        if touch:
            self._touch(row, self._is_hot(row))
        fresh = self._q("SELECT * FROM items WHERE id = ?", (iid,))
        return self._public(fresh[0] if fresh else row, data.decode("utf-8", "replace"))

    def _is_hot(self, row) -> bool:
        return bool(row["hot"] and self.cache is not None and self.cache.has(row["id"]))

    def _read_best(self, row) -> Optional[bytes]:
        if row["hot"] and self.cache is not None:
            data = self.cache.get(row["id"])
            if data is not None:
                return data
        return self._read_durable(row)

    def _read_durable(self, row) -> Optional[bytes]:
        backend = self._backend(row["tier"])
        data = backend.get(row["id"]) if backend else None
        if data is None and row["state"] == "moving" and row["move_to"]:
            other = self._backend(row["move_to"])
            data = other.get(row["id"]) if other else None
        if data is None or hashlib.sha256(data).hexdigest() != row["sha"]:
            logger.warning("context vault: item %s is missing or corrupt in tier %s; dropped from the index",
                           row["id"], row["tier"])
            self._incr("lost")
            self._forget(row["id"])
            return None
        return data

    def _forget(self, iid: str) -> None:
        for t in self.tiers:
            t.delete(iid)
        self._x("DELETE FROM items WHERE id = ?", (iid,))

    def _public(self, row, text: Optional[str] = None) -> dict:
        out = {"id": row["id"], "kind": row["kind"], "source": row["source"], "project": row["project"],
               "paths": json.loads(row["paths"] or "[]"), "tags": json.loads(row["tags"] or "[]"),
               "created": row["created"], "last_access": row["last_access"], "hits": row["hits"],
               "importance": round(float(row["importance"]), 3), "tier": row["tier"], "hot": bool(row["hot"]),
               "size": row["size"], "has_embedding": row["embedding"] is not None}
        if text is not None:
            out["text"] = text
        return out

    def _touch(self, row, hot: bool) -> None:
        self._incr("reads_hot" if hot else "reads_cold")
        self._x("UPDATE items SET hits = hits + 1, last_access = ? WHERE id = ?", (self.clock(), row["id"]))
        self._schedule_promotion(row["id"])

    def _schedule_promotion(self, iid: str) -> None:
        rows = self._q("SELECT * FROM items WHERE id = ?", (iid,))
        if not rows:
            return
        row = rows[0]
        if self.keep_score(row) < float(self.cfg.get("promote_score") or 0.35):
            return
        if self.cache is not None and not row["hot"]:
            self.submit(lambda: self.heat(iid))
        backend = self._backend(row["tier"])
        idx = self.durable.index(backend) if backend in self.durable else 0
        if idx > 0 and self.clock() - float(row["moved_at"] or 0) >= float(self.cfg.get("min_residency_s") or 0):
            self.submit(lambda: self.promote(iid))

    # ------------------------------------------------------------------ recall
    def recall(self, query: str = "", project: Optional[str] = None, kinds=None, k: int = 5,
               budget_tokens: int = 0, touch: bool = True) -> list[dict]:
        """The k best items for `query` that fit `budget_tokens` (0 = no budget). Embeddings when the vault has them
        and the embedder answers, keyword scoring otherwise."""
        if touch:
            self._incr("recalls")
        sql, args = "SELECT * FROM items WHERE state IN ('ok','moving')", []
        if kinds:
            ks = [x for x in kinds if x in KINDS]
            if not ks:
                return []
            sql += f" AND kind IN ({','.join('?' * len(ks))})"
            args += ks
        if project:
            sql += " AND (project = ? OR project = '')"
            args.append(project)
        sql += " ORDER BY last_access DESC LIMIT 5000"
        rows = self._q(sql, tuple(args))
        if not rows:
            return []
        now = self.clock()
        docs = [f"{r['preview']} {' '.join(json.loads(r['tags'] or '[]'))} {' '.join(json.loads(r['paths'] or '[]'))} {r['kind']}"
                for r in rows]
        has_query = bool(query.strip())
        kw = search.bm25(query, docs) if has_query else [0.0] * len(rows)
        top_kw = max(kw) or 1.0
        qvec = None
        if has_query and self.embedder is not None and any(r["embedding"] for r in rows):
            qvec = self._embed_one(query)
        scored = []
        for r, s in zip(rows, kw):
            kws = s / top_kw
            vec = search.unpack(r["embedding"]) if qvec is not None and r["embedding"] else None
            if has_query:
                if vec is not None:
                    cos = max(0.0, search.cosine(qvec, vec))
                    if cos < 0.3 and kws == 0:
                        continue
                    rel = 0.7 * cos + 0.3 * kws
                else:
                    if s <= 0:
                        continue
                    rel = kws
            else:
                rel = 0.0
            scored.append((rel + 0.15 * self.keep_score(r, now), r))
        scored.sort(key=lambda t: -t[0])
        out, used = [], 0
        for score, r in scored:
            if len(out) >= max(1, k):
                break
            data = self._read_best(r)
            if data is None:
                continue
            text = data.decode("utf-8", "replace")
            need = est_tokens(text)
            if budget_tokens and used + need > budget_tokens:
                if out:
                    continue
                text = text[: max(40, budget_tokens * 4)]
                need = est_tokens(text)
            hot = self._is_hot(r)
            if touch:
                self._touch(r, hot)
            fresh = self._q("SELECT * FROM items WHERE id = ?", (r["id"],))
            d = self._public(fresh[0] if fresh else r, text)
            d["score"] = round(score, 4)
            out.append(d)
            used += need
        if out and touch:
            self._incr("recalls_with_results")
        return out

    # ------------------------------------------------------------------ tier movement
    def _used(self, tier: str) -> int:
        return int(self._q("SELECT COALESCE(SUM(size), 0) AS n FROM items WHERE tier = ?", (tier,))[0]["n"])

    def _hot_used(self) -> int:
        if self.cache is None:
            return 0
        return int(self._q("SELECT COALESCE(SUM(size), 0) AS n FROM items WHERE hot = ?", (self.cache.name,))[0]["n"])

    def heat(self, iid: str) -> bool:
        """Copy an item into the RAM tier (the durable copy stays)."""
        if self.cache is None:
            return False
        rows = self._q("SELECT * FROM items WHERE id = ?", (iid,))
        if not rows or rows[0]["hot"]:
            return False
        cap = self.caps.get(self.cache.name, 0)
        if cap and rows[0]["size"] > cap:
            return False
        data = self._read_durable(rows[0])
        if data is None:
            return False
        self.cache.put(iid, data)
        self._x("UPDATE items SET hot = ? WHERE id = ?", (self.cache.name, iid))
        self.rebalance()
        return True

    def promote(self, iid: str) -> bool:
        """Move an item one durable tier up (if there is room, or it outscores what would be pushed out)."""
        with self._move_lock:
            rows = self._q("SELECT * FROM items WHERE id = ? AND state = 'ok'", (iid,))
            if not rows:
                return False
            row = rows[0]
            backend = self._backend(row["tier"])
            if backend not in self.durable or self.durable.index(backend) == 0:
                return False
            dest = self.durable[self.durable.index(backend) - 1]
            cap = self.caps.get(dest.name, 0)
            if cap and self._used(dest.name) + row["size"] > cap:
                peers = self._q("SELECT * FROM items WHERE tier = ? AND state = 'ok'", (dest.name,))
                if not peers or self.keep_score(row) <= min(self.keep_score(p) for p in peers):
                    return False
            if self._move(iid, dest.name):
                self._incr("promoted")
                self.rebalance()
                return True
            return False

    def _stage(self, name: str) -> None:
        if self._fault is not None:
            self._fault(name)

    def _move(self, iid: str, dest_name: str) -> bool:
        """The crash-safe move. False when the item is gone or its body is unreadable."""
        rows = self._q("SELECT * FROM items WHERE id = ?", (iid,))
        if not rows or rows[0]["state"] != "ok":
            return False
        row = rows[0]
        src, dest = self._backend(row["tier"]), self._backend(dest_name)
        if src is None or dest is None or src is dest:
            return False
        self._x("UPDATE items SET state = 'moving', move_to = ? WHERE id = ?", (dest_name, iid))
        data = src.get(iid)
        if data is None or hashlib.sha256(data).hexdigest() != row["sha"]:
            logger.warning("context vault: item %s unreadable in %s; dropped", iid, src.name)
            self._incr("lost")
            self._forget(iid)
            return False
        dest.put(iid, data)
        self._stage("after_copy")
        back = dest.get(iid)
        if back is None or hashlib.sha256(back).hexdigest() != row["sha"]:
            dest.delete(iid)
            self._x("UPDATE items SET state = 'ok', move_to = NULL WHERE id = ?", (iid,))
            return False
        self._x("UPDATE items SET tier = ?, state = 'ok', move_to = NULL, moved_at = ? WHERE id = ?",
                (dest_name, self.clock(), iid))
        self._stage("after_index")
        src.delete(iid)
        return True

    def demote(self, iid: str) -> bool:
        rows = self._q("SELECT tier FROM items WHERE id = ?", (iid,))
        if not rows:
            return False
        backend = self._backend(rows[0]["tier"])
        if backend not in self.durable or self.durable.index(backend) + 1 >= len(self.durable):
            return False
        with self._move_lock:
            ok = self._move(iid, self.durable[self.durable.index(backend) + 1].name)
        if ok:
            self._incr("demoted")
        return ok

    def rebalance(self) -> dict:
        """Bring every tier under its cap: shed the lowest keep scores downward, drop from the last tier."""
        report = {"demoted": 0, "evicted": 0, "cache_evictions": 0}
        target = float(self.cfg.get("demote_target") or 0.9)
        with self._move_lock:
            now = self.clock()
            for i, backend in enumerate(self.durable):
                cap = self.caps.get(backend.name, 0)
                if not cap or self._used(backend.name) <= cap:
                    continue
                last = i == len(self.durable) - 1
                rows = self._q("SELECT * FROM items WHERE tier = ? AND state = 'ok'", (backend.name,))
                used = sum(r["size"] for r in rows)
                if last:
                    order = sorted(rows, key=lambda r: (r["importance"], r["last_access"] or 0))
                else:
                    order = sorted(rows, key=lambda r: self.keep_score(r, now))
                for r in order:
                    if used <= cap * target:
                        break
                    if last:
                        logger.info("context vault: last tier %s is over its cap; dropping item %s (%s, importance %.2f)",
                                    backend.name, r["id"], r["kind"], r["importance"])
                        self._forget(r["id"])
                        self._incr("evicted")
                        report["evicted"] += 1
                        used -= r["size"]
                    elif self._move(r["id"], self.durable[i + 1].name):
                        self._incr("demoted")
                        report["demoted"] += 1
                        used -= r["size"]
            if self.cache is not None:
                cap = self.caps.get(self.cache.name, 0)
                if cap and self._hot_used() > cap:
                    rows = self._q("SELECT * FROM items WHERE hot = ?", (self.cache.name,))
                    used = sum(r["size"] for r in rows)
                    for r in sorted(rows, key=lambda r: self.keep_score(r, now)):
                        if used <= cap * target:
                            break
                        self.cache.delete(r["id"])
                        self._x("UPDATE items SET hot = NULL WHERE id = ?", (r["id"],))
                        self._incr("cache_evictions")
                        report["cache_evictions"] += 1
                        used -= r["size"]
        return report

    # ------------------------------------------------------------------ recovery
    OWNER_FILE = ".vault-index"

    def _owns(self, backend, stray: list) -> bool:
        """May recover() delete the files in `backend` that this index does not know? Only in a tier folder this vault owns.
        Tier folders are absolute paths from the config, so a second ABP with its own data folder (a test instance, a staged
        bundle) shares them; its sweep used to delete the live vault's items (2026-10-20: 13 of 22 lost). A folder records
        its owner's index in .vault-index; an unmarked folder is claimed only when nothing in it is foreign."""
        root = getattr(backend, "root", None)
        if root is None:
            return True
        marker = Path(root) / self.OWNER_FILE
        me = str(self.index_file.resolve())
        try:
            owner = marker.read_text(encoding="utf-8").strip()
        except OSError:
            owner = ""
        if owner and Path(owner) != Path(me):
            if stray:
                logger.warning("context vault: tier %s (%s) belongs to the vault at %s; its %d item(s) unknown here are left alone",
                               backend.name, root, owner, len(stray))
            return False
        if not owner:
            if stray:
                logger.warning("context vault: tier %s (%s) holds %d item(s) this vault does not know and has no owner mark; "
                               "left alone (another vault may share the folder)", backend.name, root, len(stray))
                return False
            try:
                marker.write_text(me, encoding="utf-8")
            except OSError:
                pass
        return True

    def recover(self) -> dict:
        """Finish or roll back moves a crash left half done, forget RAM copies, sweep leftovers."""
        out = {"completed": 0, "rolled_back": 0, "lost": 0, "swept": 0}
        with self._move_lock:
            self._x("UPDATE items SET hot = NULL")
            for r in self._q("SELECT * FROM items WHERE state = 'moving'"):
                src, dest = self._backend(r["tier"]), self._backend(r["move_to"])
                d_data = dest.get(r["id"]) if dest else None
                s_data = src.get(r["id"]) if src else None

                def good(b, sha=r["sha"]):
                    return b is not None and hashlib.sha256(b).hexdigest() == sha

                if dest is not None and good(d_data):
                    self._x("UPDATE items SET tier = ?, state = 'ok', move_to = NULL, moved_at = ? WHERE id = ?",
                            (dest.name, self.clock(), r["id"]))
                    if src is not None:
                        src.delete(r["id"])
                    out["completed"] += 1
                elif good(s_data):
                    if dest is not None:
                        dest.delete(r["id"])
                    self._x("UPDATE items SET state = 'ok', move_to = NULL WHERE id = ?", (r["id"],))
                    out["rolled_back"] += 1
                else:
                    self._forget(r["id"])
                    out["lost"] += 1
            for backend in self.durable:
                out["swept"] += backend.clear_partial()
                stray = []
                for key in backend.keys():
                    rows = self._q("SELECT tier FROM items WHERE id = ?", (key,))
                    if not rows or rows[0]["tier"] != backend.name:
                        stray.append(key)
                if not self._owns(backend, stray):
                    continue
                for key in stray:
                    backend.delete(key)
                    out["swept"] += 1
        if out["completed"] or out["rolled_back"] or out["lost"]:
            self._incr("recovered", out["completed"] + out["rolled_back"])
            if out["lost"]:
                self._incr("lost", out["lost"])
            logger.info("context vault recovery: %s", out)
        return out

    # ------------------------------------------------------------------ background work
    def submit(self, fn: Callable[[], Any]) -> bool:
        """Queue work for the worker thread (or `drain()`); the queue is bounded, the oldest work is dropped."""
        limit = int((self.cfg.get("capture") or {}).get("queue_max") or 200)
        while self._tasks.qsize() >= limit:
            try:
                self._tasks.get_nowait()
                self._incr("dropped")
            except queue.Empty:
                break
        self._tasks.put(fn)
        return True

    def drain(self) -> int:
        """Run all queued work in the calling thread (CLI, tests)."""
        n = 0
        while True:
            try:
                fn = self._tasks.get_nowait()
            except queue.Empty:
                return n
            self._run(fn)
            n += 1

    @staticmethod
    def _run(fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - background work never takes the thread down
            logger.exception("context vault: background task failed")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="context-vault", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        interval = max(1.0, float(self.cfg.get("maintenance_interval_s") or 60))
        while not self._stop.is_set():
            try:
                fn = self._tasks.get(timeout=min(interval, 1.0))
            except queue.Empty:
                if time.monotonic() - self._last_maint >= interval:
                    self._last_maint = time.monotonic()
                    self._run(self.rebalance)
                continue
            self._run(fn)

    # ------------------------------------------------------------------ reporting
    def stats(self) -> dict:
        counters = {n: 0 for n in COUNTERS}
        for r in self._q("SELECT name, value FROM counters"):
            counters[r["name"]] = r["value"]
        tiers = []
        for t in self.tiers:
            col = "hot" if t.volatile else "tier"
            rows = self._q(f"SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS b FROM items WHERE {col} = ?", (t.name,))
            tiers.append({"name": t.name, "volatile": t.volatile, "items": rows[0]["c"], "bytes": rows[0]["b"],
                          "max_bytes": self.caps.get(t.name, 0), "path": str(getattr(t, "root", ""))})
        total = self._q("SELECT COUNT(*) AS c FROM items")[0]["c"]
        recalls = counters["recalls"]
        return {"enabled": bool(self.cfg.get("enabled")), "items": total, "tiers": tiers, "counters": counters,
                "hit_rate": round(counters["recalls_with_results"] / recalls, 3) if recalls else None,
                "queued": self._tasks.qsize(), "embeddings": self.embedder is not None}

    def list_items(self, limit: int = 50, kind: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM items", ()
        if kind:
            sql, args = sql + " WHERE kind = ?", (kind,)
        return [self._public(r) for r in self._q(sql + " ORDER BY last_access DESC LIMIT ?", args + (int(limit),))]

    def export_items(self) -> list[dict]:
        out = []
        for r in self._q("SELECT id FROM items ORDER BY created"):
            item = self.get(r["id"], touch=False)
            if item:
                out.append(item)
        return out

    def import_items(self, items: list[dict]) -> dict:
        res = {"stored": 0, "deduped": 0, "skipped": 0}
        for it in items:
            if not isinstance(it, dict):
                continue
            r = self.add(str(it.get("text") or ""), str(it.get("kind") or "note"), source=str(it.get("source") or ""),
                         project=str(it.get("project") or ""), paths=it.get("paths") or (), tags=it.get("tags") or (),
                         importance=it.get("importance"), created=it.get("created"))
            res[r["status"]] = res.get(r["status"], 0) + 1
        return res

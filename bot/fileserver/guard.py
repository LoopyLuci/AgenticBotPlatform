"""Guard: notices ransomware-like activity on the shares and can freeze them before more is lost.

Each check compares the shares with the last check and scores what changed:
    rate        files changed per minute against this server's own normal (an exponentially weighted mean and
                deviation it learns as it runs): a z-score
    entropy     changed files whose first 64 KiB went from structured (< 6 bits/byte) to near random (> 7.5): what
                encryption does to documents, text and code (already-compressed media is not counted)
    renames     files that vanished while a same-sized file with an unfamiliar new extension appeared
    notes       ransom-note names (README_DECRYPT, HOW_TO_RECOVER_FILES, ...)
The signals add to a score in 0..1. Over `alert_at` it raises an alert (notifications, the Storage page, agents); with
`auto_freeze` on, the affected shares become read-only for everyone until a person unfreezes them.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Callable

from bot.fileserver import shares
from bot.fileserver.store import db, load, update

Log = Callable[[str], None]
NOTE = re.compile(r"(readme|how|help|recover|restore|decrypt|ransom|your[_ -]?files)[^/]*\.(txt|html?|hta|url)$", re.I)
COMPRESSED = {"jpg", "jpeg", "png", "gif", "webp", "mp4", "mkv", "mov", "mp3", "flac", "ogg", "m4a", "zip", "7z", "rar",
              "gz", "xz", "bz2", "zst", "pdf", "docx", "xlsx", "pptx", "heic", "avif", "webm"}


def settings() -> dict:
    return {"enabled": True, "alert_at": 0.6, "auto_freeze": False, **load("guard", {})}


def _con():
    con = db("guard")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS seen(share TEXT, path TEXT, size INT, mtime_ns INT, entropy REAL, PRIMARY KEY(share, path));
        CREATE TABLE IF NOT EXISTS alerts(at INT, share TEXT, score REAL, detail TEXT, frozen INT, cleared INT);
        CREATE TABLE IF NOT EXISTS baseline(share TEXT PRIMARY KEY, mean REAL, var REAL, n INT, at INT);
    """)
    return con


def entropy(path: Path, n: int = 65536) -> float:
    try:
        with open(path, "rb") as f:
            data = f.read(n)
    except OSError:
        return -1.0
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    return -sum(c / total * math.log2(c / total) for c in counts.values())


def frozen() -> set[str]:
    return set(load("frozen", []))


def freeze(names: list[str]) -> list[str]:
    update("frozen", [], lambda f: f.extend(n for n in names if n not in f))
    return sorted(frozen())


def unfreeze(names: list[str] | None = None) -> list[str]:
    def put(f):
        keep = [n for n in f if names is not None and n not in names]
        f.clear()
        f.extend(keep)
    update("frozen", [], put)
    con = _con()
    with con:
        con.execute("UPDATE alerts SET cleared=? WHERE cleared IS NULL", (int(time.time()),))
    con.close()
    return sorted(frozen())


def check(log: Log = lambda m: None) -> list[dict]:
    st = settings()
    if not st["enabled"]:
        return []
    con = _con()
    alerts = []
    now = time.time()
    for name, s in shares.shares().items():
        s = {**s, "name": name}
        prev = {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT path, size, mtime_ns, entropy FROM seen WHERE share=?", (name,))}
        first = not prev
        cur = {}
        for rel, real in shares.walk(s):
            try:
                stt = real.stat()
            except OSError:
                continue
            cur[rel] = (stt.st_size, stt.st_mtime_ns, real)
        changed = [p for p, v in cur.items() if p in prev and (prev[p][0], prev[p][1]) != v[:2]]
        added = [p for p in cur if p not in prev]
        removed = [p for p in prev if p not in cur]
        ent_new: dict[str, float] = {}
        jumps = 0
        for p in changed + added:
            ext = p.rsplit(".", 1)[-1].lower() if "." in p else ""
            e = entropy(cur[p][2])
            ent_new[p] = e
            old = prev.get(p, (0, 0, -1))[2]
            if p in prev and ext not in COMPRESSED and 0 <= old < 6.0 and e > 7.5:
                jumps += 1
        removed_sizes = Counter(prev[p][0] for p in removed)
        old_exts = {p.rsplit(".", 1)[-1].lower() for p in prev if "." in p}
        renames = 0
        for p in added:
            ext = p.rsplit(".", 1)[-1].lower() if "." in p else ""
            if ext and ext not in old_exts and removed_sizes.get(cur[p][0]):
                removed_sizes[cur[p][0]] -= 1
                renames += 1
        notes = [p for p in added if NOTE.search(p)]
        b = con.execute("SELECT mean, var, n, at FROM baseline WHERE share=?", (name,)).fetchone()
        activity = len(changed) + len(added) + len(removed)
        mins = max(1.0, (now - (b[3] if b else now - 60)) / 60)
        rate = activity / mins
        mean, var, n = (b[0], b[1], b[2]) if b else (rate, 1.0, 0)
        z = (rate - mean) / math.sqrt(var + 1.0) if n >= 5 else 0.0
        touched = max(1, len(changed) + len(added))
        score = 0.0
        score += min(0.35, max(0.0, z - 3) / 20)
        score += min(0.4, jumps / max(5, touched) * 0.8) if jumps >= 3 else 0
        score += min(0.3, renames / max(5, touched) * 0.6) if renames >= 3 else 0
        score += 0.3 if notes and (jumps or renames) else (0.1 if notes else 0)
        score = round(min(1.0, score), 3)
        if not first and score >= st["alert_at"]:
            detail = {"rate_per_min": round(rate, 1), "normal_per_min": round(mean, 1), "z": round(z, 1), "entropy_jumps": jumps,
                      "renamed_to_new_extensions": renames, "ransom_notes": notes[:10], "changed": len(changed),
                      "added": len(added), "removed": len(removed), "examples": (changed + added)[:10]}
            fz = bool(st["auto_freeze"])
            if fz:
                freeze([name])
            import json
            con.execute("INSERT INTO alerts(at, share, score, detail, frozen, cleared) VALUES(?,?,?,?,?,NULL)",
                        (int(now), name, score, json.dumps(detail), int(fz)))
            alerts.append({"share": name, "score": score, "frozen": fz, **detail})
            log(f"guard: ALERT on {name} (score {score}): {detail}")
        if not first and score < 0.3:          # learn the normal rate only from calm periods
            a = 0.1
            mean_n = (1 - a) * mean + a * rate
            var_n = (1 - a) * (var + a * (rate - mean) ** 2)
            con.execute("INSERT OR REPLACE INTO baseline(share, mean, var, n, at) VALUES(?,?,?,?,?)", (name, mean_n, var_n, n + 1, int(now)))
        elif first or not b:
            con.execute("INSERT OR REPLACE INTO baseline(share, mean, var, n, at) VALUES(?,?,?,?,?)", (name, 0.0, 1.0, 0, int(now)))
        else:
            con.execute("UPDATE baseline SET at=? WHERE share=?", (int(now), name))
        with con:
            con.executemany("DELETE FROM seen WHERE share=? AND path=?", [(name, p) for p in removed])
            con.executemany("INSERT OR REPLACE INTO seen(share, path, size, mtime_ns, entropy) VALUES(?,?,?,?,?)",
                            [(name, p, cur[p][0], cur[p][1], ent_new.get(p, prev.get(p, (0, 0, -1))[2] if p in prev else entropy(cur[p][2])))
                             for p in (changed + added if not first else cur)])
    con.commit()
    con.close()
    return alerts


def recent_alerts(limit: int = 50) -> list[dict]:
    import json
    con = _con()
    rows = con.execute("SELECT at, share, score, detail, frozen, cleared FROM alerts ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [{"at": r[0], "share": r[1], "score": r[2], "detail": json.loads(r[3]), "frozen": bool(r[4]), "cleared": r[5]} for r in rows]

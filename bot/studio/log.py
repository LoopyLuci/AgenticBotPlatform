"""Studio's log: every step as one JSON line in data/studio/log.jsonl (create, edit, generate, validate, shot, rate,
apply, revert, discard), and the datasets built from it for ABP's own models (roadmap GEN-D / GEN-1):

    sft.jsonl    {"instruction", "files", "diff", "model"}: changes people kept (applied, or rated 4-5)
    prefs.jsonl  {"instruction", "chosen", "rejected"}: two variants for the same request, one kept, one not

Nothing leaves this machine."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()
MAX_LOG = 64 * 1024 * 1024                    # the log rolls over (log.1.jsonl) past this size


def _path() -> Path:
    from bot.studio.variants import studio_dir
    return studio_dir() / "log.jsonl"


def event(kind: str, **fields: Any) -> dict:
    rec = {"t": round(time.time(), 3), "kind": kind, **fields}
    p = _path()
    with _lock:
        if p.is_file() and p.stat().st_size > MAX_LOG:
            p.replace(p.with_name("log.1.jsonl"))
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    return rec


def read(limit: int = 500, kind: str = "") -> list[dict]:
    p = _path()
    if not p.is_file():
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not kind or rec.get("kind") == kind:
                out.append(rec)
    return out[-limit:]


def datasets() -> dict:
    """Build sft.jsonl and prefs.jsonl (in data/studio/datasets) from the log; returns their paths and sizes."""
    from bot.studio.variants import studio_dir
    recs = read(limit=10**9)
    requests: dict[str, dict] = {}                    # vid -> the generate request that made it
    outcome: dict[str, int] = {}                      # vid -> +1 kept, -1 not kept
    diffs: dict[str, list[str]] = {}
    models: dict[str, str] = {}
    for r in recs:
        vid = r.get("vid")
        if not vid:
            continue
        if r["kind"] == "generate":
            requests[vid] = {"instruction": r.get("instruction", ""), "files": r.get("files", []), "group": r.get("group")}
            models[vid] = r.get("model", "")
        elif r["kind"] in ("edit", "generate-edit") and r.get("diff"):
            diffs.setdefault(vid, []).append(r["diff"])
        elif r["kind"] == "apply":
            outcome[vid] = 1
        elif r["kind"] == "rate":
            outcome[vid] = 1 if r.get("rating", 0) >= 4 else -1 if r.get("rating", 5) <= 2 else outcome.get(vid, 0)
        elif r["kind"] == "discard":
            outcome.setdefault(vid, -1)
    out = studio_dir() / "datasets"
    out.mkdir(parents=True, exist_ok=True)
    sft, prefs = [], []
    by_group: dict[str, list[str]] = {}
    for vid, req in requests.items():
        by_group.setdefault(req.get("group") or vid, []).append(vid)
        if outcome.get(vid) == 1 and diffs.get(vid):
            sft.append({"instruction": req["instruction"], "files": req["files"], "diff": "\n".join(diffs[vid]),
                        "model": models.get(vid, "")})
    for vids in by_group.values():
        kept = [v for v in vids if outcome.get(v) == 1 and diffs.get(v)]
        dropped = [v for v in vids if outcome.get(v) == -1 and diffs.get(v)]
        for k in kept:
            for d in dropped:
                prefs.append({"instruction": requests[k]["instruction"], "chosen": "\n".join(diffs[k]),
                              "rejected": "\n".join(diffs[d]), "chosen_model": models.get(k, ""),
                              "rejected_model": models.get(d, "")})
    files = {}
    for name, rows in (("sft.jsonl", sft), ("prefs.jsonl", prefs)):
        p = out / name
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        files[name] = {"path": str(p), "rows": len(rows)}
    return files

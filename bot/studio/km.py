"""Knowledge Modules from Studio's applied changes (roadmap GEN-D).

export()  one markdown document per applied variant in data/studio/km/source/: the request, the files changed, the diff
          (only those files), the rating, whether the change was kept or reverted, the gate result. Every document is
          scrubbed of credentials and machine paths and checked with the leak guard's rules; one that still trips a rule
          is left out. Nothing outside the changed UI files is ever read.
build()   asks a running ModelMistress hub to make a data + memory KM from that folder (`factory.task.create`). The task
          stops for a person to approve the plan, and its promotion is only proposed (`approval: propose`): ABP never
          promotes a KM itself. Refused when no hub runs or there is nothing to export.
status()  the recorded tasks, each polled with `factory.task.get`.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from bot.studio.variants import StudioError, UI_EXTS, UI_ROOTS, studio_dir

DOMAIN = "abp/studio-ui"
GOAL = ("How ABP's interface was changed through Studio: for each request, which UI files were edited, the diff, how a "
        "person rated it, whether it was kept, and whether the checks passed.")
MAX_DIFF = 12000                        # characters of diff per file in a document
_lock = threading.Lock()
_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s\"'`,;|)(<>]*"
                   r"|(?<![\w.])/(?:home|Users|root|mnt|srv|var|opt)/[^\s\"'`,;|)(<>]*")


def km_dir() -> Path:
    d = studio_dir() / "km"
    (d / "source").mkdir(parents=True, exist_ok=True)
    return d


def source_dir() -> Path:
    return km_dir() / "source"


def _leak_rules():
    from bot.envfile import CODE_ROOT
    path = CODE_ROOT / "scripts" / "leak_guard.py"
    if not path.is_file():
        return None, []
    spec = importlib.util.spec_from_file_location("studio_km_leak_guard", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = mod            # its dataclasses look themselves up here
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    finally:
        sys.modules.pop(spec.name, None)
    return mod, mod._build_rules(None)


def scrub(text: str) -> str:
    """Credentials (known shapes, key=value forms, this process's own secrets) and machine paths removed."""
    from bot.agent_runtime import secrets_guard
    from bot.diagnostics import redact
    return _PATH.sub("<path>", redact(secrets_guard.redact(str(text or ""))))


def _ui_file(rel: str) -> bool:
    r = str(rel or "").replace("\\", "/")
    return (any(r.startswith(root + "/") for root in UI_ROOTS) and r.endswith(UI_EXTS)
            and ".." not in r.split("/") and ":" not in r)


def _fence(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return fence


def _sessions(recs: list[dict]) -> list[dict]:
    """One entry per variant that was applied (the log is the only source), oldest first."""
    gen: dict[str, dict] = {}
    out: dict[str, dict] = {}
    edits: dict[str, list[dict]] = {}
    ratings: dict[str, dict] = {}
    gates: dict[str, dict] = {}
    for r in recs:
        vid = r.get("vid")
        if not vid:
            continue
        k = r.get("kind")
        if k == "generate":
            gen[vid] = r
        elif k in ("edit", "generate-edit") and r.get("diff"):
            edits.setdefault(vid, []).append(r)
        elif k == "rate":
            ratings[vid] = r
        elif k == "gate":
            gates[vid] = r
        elif k == "apply":
            out[vid] = {"vid": vid, "t": r.get("t", 0), "files": [f for f in r.get("files") or [] if _ui_file(f)],
                        "reverted": False}
        elif k == "revert" and vid in out and r.get("t", 0) >= out[vid]["t"]:
            out[vid]["reverted"] = True
    sessions = []
    for vid, s in out.items():
        g = gen.get(vid) or {}
        s.update(instruction=g.get("instruction") or "", model=g.get("model") or "", rating=ratings.get(vid),
                 gate=gates.get(vid), edits=[e for e in edits.get(vid, []) if e.get("file") in s["files"]])
        if s["files"]:
            sessions.append(s)
    return sorted(sessions, key=lambda s: s["t"])


def _document(s: dict) -> str:
    when = time.strftime("%Y-%m-%d", time.gmtime(s["t"]))
    rating = s["rating"]
    gate = s["gate"]
    lines = [f"# Studio change {s['vid']}", "",
             f"Request: {s['instruction'] or '(written by hand, no request recorded)'}", "",
             f"Applied: {when}. Outcome: {'reverted afterwards' if s['reverted'] else 'kept'}."]
    if s["model"]:
        lines.append(f"Proposed by: {s['model']}.")
    lines.append(f"Rating: {rating['rating']} of 5" + (f" ({rating.get('why')})" if rating.get("why") else "")
                 if rating else "Rating: none")
    lines.append("Checks: " + ("not run" if not gate else "passed" if gate.get("passed")
                              else "failed (" + ", ".join(gate.get("failed") or []) + ")"))
    lines += ["", "Files changed:"] + [f"- {f}" for f in s["files"]]
    by_file: dict[str, list[str]] = {}
    for e in s["edits"]:
        by_file.setdefault(e["file"], []).append(e["diff"])
    for f in s["files"]:
        for d in by_file.get(f, []):
            d = d[:MAX_DIFF]
            fence = _fence(d)
            lines += ["", f"## {f}", "", f"{fence}diff", d.rstrip("\n"), fence]
    return scrub("\n".join(lines) + "\n")


def export(datasets: bool = False) -> dict:
    """Rewrite the KM source folder from the log. Returns {dir, documents, skipped, variants, datasets?}."""
    from bot.studio import log
    sessions = _sessions(log.read(limit=10 ** 9))
    out = source_dir()
    mod, rules = _leak_rules()
    written, skipped = [], []
    with _lock:
        for old in out.glob("*.md"):                  # this folder is ours: only documents an export made
            old.unlink()
        for s in sessions:
            doc = _document(s)
            if mod is not None and any(mod.scan_text(f"{s['vid']}.md", doc, rules)):
                skipped.append(s["vid"])
                continue
            (out / f"{s['vid']}.md").write_text(doc, encoding="utf-8")
            written.append(s["vid"])
    res: dict[str, Any] = {"dir": str(out), "documents": len(written), "skipped": skipped, "variants": written}
    if datasets:
        res["datasets"] = log.datasets()
    return res


def _tasks() -> list[dict]:
    p = km_dir() / "tasks.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [t for t in data if isinstance(t, dict) and t.get("task")] if isinstance(data, list) else []


def _save(tasks: list[dict]) -> None:
    (km_dir() / "tasks.json").write_text(json.dumps(tasks[-50:], indent=1), encoding="utf-8")


def task_json(folder: str) -> dict:
    return {"goal": GOAL, "kinds": ["data", "memory"], "domain": DOMAIN,
            "sources": [{"kind": "dir", "path": folder, "globs": ["*.md"], "max_files": 500}],
            "plan_approval": True, "approval": "propose",
            "size": {"records": 100, "max_tokens": 1000000}, "budget": {"wall_s": 3600, "max_records": 100}}


def build(datasets: bool = False) -> dict:
    """Export, then create the factory task. The plan and the promotion wait for a person in ModelMistress."""
    from bot.modelmistress import client
    if client.find(refresh=True) is None:
        raise StudioError("ModelMistress is not running: start its hub from the Modules page, then build again")
    res = export(datasets)
    if not res["documents"]:
        raise StudioError("nothing to build from: apply a Studio change first (the export has no documents)")
    try:
        made = client.call("factory.task.create", {**task_json(res["dir"]), "start": True})
    except client.MMError as e:
        raise StudioError(f"ModelMistress refused the task: {e}") from None
    made = made if isinstance(made, dict) else {}
    tid = str(made.get("task") or made.get("id") or "")
    if not tid:
        raise StudioError("ModelMistress did not return a task id")
    rec = {"task": tid, "job": made.get("job"), "state": made.get("state") or "created", "created": time.time(),
           "documents": res["documents"], "export": res["dir"]}
    with _lock:
        _save(_tasks() + [rec])
    return {**rec, "approval": "propose", "plan_approval": True}


_NEEDS = {
    "awaiting_approval": "Approve the plan in ModelMistress to let it continue.",
    "budget": "The budget ran out; raise it in ModelMistress and resume.",
    "interrupted": "The hub stopped while it ran; resume it in ModelMistress.",
    "failed": "The task failed; see its log in ModelMistress.",
}


def _one(rec: dict, hub) -> dict:
    from bot.modelmistress import client
    out = {"task": rec["task"], "created": rec.get("created"), "documents": rec.get("documents"),
           "state": rec.get("state") or "created", "needs": "", "open_reviews": None, "error": ""}
    if hub is None:
        out["error"] = "ModelMistress is not running"
        return out
    try:
        got = client.call("factory.task.get", {"id": rec["task"]}, hub=hub, timeout=20.0)
    except client.MMError as e:
        out["error"] = str(e)[:300]
        return out
    got = got if isinstance(got, dict) else {}
    state = str(got.get("state") or out["state"])
    out["state"] = state
    out["needs"] = _NEEDS.get(state, "")
    if state in ("done", "running"):
        try:
            q = client.call("factory.review.list", {"task": rec["task"], "state": "open", "limit": 50}, hub=hub, timeout=20.0)
            items = q.get("items") if isinstance(q, dict) else q
            out["open_reviews"] = len(items) if isinstance(items, list) else None
        except client.MMError:
            pass
        if state == "done":
            out["needs"] = ("Review the open items and the promotion in ModelMistress." if out["open_reviews"]
                            else "Finished: the KM waits as a candidate; promote it in ModelMistress if you want it.")
    out["review_url"] = hub.url if str(hub.url).startswith(("http://", "https://")) else ""  # only ever an http(s) link
    return out


def status(task: str = "") -> dict:
    from bot.modelmistress import client
    recs = [t for t in _tasks() if not task or t["task"] == task]
    hub = client.find() if recs else None
    items = [_one(t, hub) for t in reversed(recs)]
    with _lock:                                   # remember the newest state seen
        known = {i["task"]: i["state"] for i in items if not i["error"]}
        allrecs = _tasks()
        for t in allrecs:
            t["state"] = known.get(t["task"], t.get("state"))
        if allrecs:
            _save(allrecs)
    return {"hub": hub is not None, "tasks": items,
            "export": {"dir": str(source_dir()), "documents": len(list(source_dir().glob("*.md")))}}

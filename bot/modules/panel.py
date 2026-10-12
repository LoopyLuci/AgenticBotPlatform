"""A module's panel (Module Management Hub, MH-D): a declarative layout, never generated code.

The panel itself is one hand-written renderer (module-panel.js) that shows any module's status, its operations as forms
built from their typed inputs, and the results. What differs per module is only a layout spec, `abp-panel.json`, kept
beside the module's files (its overlay, or the project for a module that has none), so a refresh or re-adopt never
touches it:

    {"version": 1, "title": "...",
     "sections": [{"title": "...", "note": "...",
                   "operations": [{"id": "script.alpha", "label": "...", "output": "table|text|json"}]}]}

Which operations to feature, how to group and name them, and how to show their result: nothing else. No HTML, no script,
no command. The spec is checked strictly (known keys only, operations that exist, the three output kinds, length caps),
both when a model proposes one (bot.modules.assist) and when it is read back. A stored file that no longer passes is
ignored (the default layout is shown, with the reasons).

With no spec the default layout groups the operations by the part of their id before the first dot."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from bot.modules import registry
from bot.modules.client import ModuleError

SPEC_FILE = "abp-panel.json"
VERSION = 1
OUTPUT_KINDS = ("table", "text", "json")
DEFAULT_OUTPUT = "json"
MAX_FILE_BYTES = 40_000
MAX_TITLE = 80
MAX_SECTION_TITLE = 60
MAX_NOTE = 200
MAX_LABEL = 60
MAX_SECTIONS = 12
MAX_OPS_PER_SECTION = 30
MAX_OPS = 60
_OP_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,120}")


# ---- checking a spec -------------------------------------------------------------------------------------------------

def _text(v: Any, cap: int, what: str, problems: list[str]) -> str:
    if not isinstance(v, str) or not v.strip():
        problems.append(f"{what}: text expected")
        return ""
    v = re.sub(r"\s+", " ", v).strip()
    if len(v) > cap:
        problems.append(f"{what}: longer than {cap} characters")
    return v


def _keys(obj: Any, allowed: set[str], what: str, problems: list[str]) -> bool:
    if not isinstance(obj, dict):
        problems.append(f"{what}: an object expected")
        return False
    extra = sorted(set(obj) - allowed)
    if extra:
        problems.append(f"{what}: not allowed here: {', '.join(map(str, extra))}")
    return True


def validate_spec(spec: Any, op_ids: Optional[Iterable[str]]) -> tuple[Optional[dict], list[str]]:
    """(the cleaned spec, problems). Problems empty = acceptable. `op_ids` is the set of operations that exist
    (None: not known, only the id's shape is checked)."""
    problems: list[str] = []
    if not _keys(spec, {"version", "title", "sections"}, "panel", problems):
        return None, problems
    known = None if op_ids is None else set(op_ids)
    if spec.get("version", VERSION) != VERSION:
        problems.append(f"panel: version must be {VERSION}")
    out: dict[str, Any] = {"version": VERSION}
    if "title" in spec:
        out["title"] = _text(spec["title"], MAX_TITLE, "title", problems)
    secs = spec.get("sections")
    if not isinstance(secs, list) or not secs:
        return None, problems + ["sections: a non-empty list expected"]
    if len(secs) > MAX_SECTIONS:
        problems.append(f"sections: more than {MAX_SECTIONS}")
        secs = secs[:MAX_SECTIONS]
    seen: set[str] = set()
    clean_secs = []
    for i, s in enumerate(secs):
        where = f"sections[{i}]"
        if not _keys(s, {"title", "note", "operations"}, where, problems):
            continue
        sec: dict[str, Any] = {"title": _text(s.get("title"), MAX_SECTION_TITLE, f"{where} title", problems)}
        if "note" in s:
            sec["note"] = _text(s["note"], MAX_NOTE, f"{where} note", problems)
        ops = s.get("operations")
        if not isinstance(ops, list) or not ops:
            problems.append(f"{where}: operations: a non-empty list expected")
            continue
        if len(ops) > MAX_OPS_PER_SECTION:
            problems.append(f"{where}: more than {MAX_OPS_PER_SECTION} operations")
            ops = ops[:MAX_OPS_PER_SECTION]
        rows = []
        for j, e in enumerate(ops):
            w = f"{where}.operations[{j}]"
            if not _keys(e, {"id", "label", "output"}, w, problems):
                continue
            oid = e.get("id")
            if not isinstance(oid, str) or not _OP_ID.fullmatch(oid) or (known is not None and oid not in known):
                problems.append(f"{w}: {oid!r} is not an operation of this module (no new operations)")
                continue
            if oid in seen:
                problems.append(f"{w}: {oid} appears twice")
                continue
            seen.add(oid)
            row: dict[str, Any] = {"id": oid}
            if "label" in e:
                row["label"] = _text(e["label"], MAX_LABEL, f"{w} label", problems)
            if "output" in e:
                if e["output"] not in OUTPUT_KINDS:
                    problems.append(f"{w}: output must be one of {', '.join(OUTPUT_KINDS)}")
                    continue
                row["output"] = e["output"]
            rows.append(row)
        if rows:
            sec["operations"] = rows
            clean_secs.append(sec)
    if len(seen) > MAX_OPS:
        problems.append(f"more than {MAX_OPS} operations in all")
    if not clean_secs and not problems:
        problems.append("the panel shows no operation")
    out["sections"] = clean_secs
    return (out if not problems else None), problems


def describe(spec: dict) -> list[str]:
    """One line per section, for a difference view: `Title: a, b (table)`."""
    lines = []
    for s in spec.get("sections", []):
        ops = ", ".join(o["id"] + (f" ({o['output']})" if o.get("output") else "") for o in s["operations"])
        lines.append(f"{s['title']}: {ops}")
    return lines


# ---- the default layout ----------------------------------------------------------------------------------------------

def default_layout(op_ids: Iterable[str]) -> dict:
    """Operations grouped by the part of their id before the first dot, in order, within the caps."""
    groups: dict[str, list[str]] = {}
    for oid in list(op_ids)[:MAX_OPS]:
        groups.setdefault(oid.split(".", 1)[0] if "." in oid else "other", []).append(oid)
    secs = [{"title": g[:MAX_SECTION_TITLE], "operations": [{"id": o} for o in ids[:MAX_OPS_PER_SECTION]]}
            for g, ids in list(groups.items())[:MAX_SECTIONS]]
    return {"version": VERSION, "sections": secs}


# ---- reading and writing ---------------------------------------------------------------------------------------------

def _get(mid: str):
    try:
        return registry.get(mid)
    except KeyError as e:
        raise ModuleError(str(e.args[0]) if e.args else f"no module {mid!r}", code="not_found", status=404) from None


def _home(m) -> Optional[Path]:
    """Where the module's own files are: its overlay, or its project; None for a built-in module."""
    if m.overlay:
        return Path(m.overlay)
    if m.source and m.source != "builtin":
        return registry.install_dir(m)
    return None


def _op_ids(home: Optional[Path]) -> Optional[list[str]]:
    if home is None or not (home / "abp-ops.toml").is_file():
        return None
    from abp_modkit import spec as sp
    try:
        return [o.id for o in sp.load(home / "abp-ops.toml").ops]
    except Exception:  # noqa: BLE001 - an unreadable ops file: the ids are unknown, not a reason to fail
        return None


def layout(mid: str) -> dict:
    """The panel layout of a module: its stored spec, or the default. Never raises for a bad stored file."""
    m = _get(mid)
    home = _home(m)
    ids = _op_ids(home)
    problems: list[str] = []
    spec = None
    p = home / SPEC_FILE if home else None
    if p is not None and p.is_file():
        try:
            raw = p.read_bytes()
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError(f"larger than {MAX_FILE_BYTES} bytes")
            spec, problems = validate_spec(json.loads(raw.decode("utf-8")), ids)
        except (OSError, ValueError) as e:
            problems = [f"{SPEC_FILE}: {e}"]
    return {"id": m.id, "name": m.name, "source": "spec" if spec else "default",
            "layout": spec or default_layout(ids or []), "problems": problems,
            "has_spec": bool(spec), "operations_known": ids is not None}


def write(home: Path, spec: dict) -> str:
    """Store a (validated) spec beside the module's files; returns the file name."""
    text = json.dumps(spec, indent=1, ensure_ascii=False) + "\n"
    (home / SPEC_FILE).write_text(text, encoding="utf-8", newline="\n")
    return SPEC_FILE

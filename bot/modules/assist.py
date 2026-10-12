"""Assisted setup (Module Management Hub, MH-D): the Hub proposes, the user picks, nothing is kept unseen.

For a module with module files (an adopted project or an overlay), the model is asked (through the same model-call
path Studio uses, over ABP's free and local models with fallback) for a few proposals, each:

    {"description": "...", "operations": [{"id": "script.alpha", "summary": "...",
                                            "inputs": [{"name": "n", "type": "integer", "required": false,
                                                        "help": "...", "choices": [1, 2]}]}]}

What the model sees is capped and scrubbed: the README, the manifest, the list of detected operations and a few of
the source files those operations run. Files that look like secrets are never read, and every text goes through
bot.diagnostics.redact first.

A proposal is checked strictly before anyone sees it. Only descriptive fields exist in the schema (the module
description, an operation's summary, and for an input that already exists its type, required, help and choices); an
unknown key, an operation that does not exist, an input that is not a real parameter of the operation (no input is
ever added: that would be a new way to run something), a bad type or an over-long text rejects the proposal. Rejected
proposals are listed with their reasons and cannot be kept.

Proposals are stored (data/studio/assist/<module>/<batch>.json) and shown as a list of differences against the
current manifest and operations. `keep` writes one chosen proposal into the module's abp-ops.toml (and the generated
manifest's description) as plain hand edits, which a later refresh or re-adopt keeps. If the module files changed
since the batch was made, keep refuses. Nothing is written by generating.

A proposal may also carry a "panel": a layout spec for the module's panel (bot.modules.panel: which operations to feature,
sections, labels, how to show each result), checked by panel.validate_spec against the operations that exist. `keep`
stores it as abp-panel.json beside the module files. `generate(panel=True)` asks for layouts only."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any, Optional

from bot.modules import registry
from bot.modules.client import ModuleError

MAX_README = 6000
MAX_MANIFEST = 2500
MAX_FILE = 3000
MAX_FILES = 4
MAX_OPS_IN_PROMPT = 60
MAX_DESCRIPTION = 400
MAX_SUMMARY = 200
MAX_HELP = 160
MAX_CHOICES = 30
MAX_PROPOSALS = 5
INPUT_TYPES = ("string", "integer", "number", "boolean")
PER_CALL_TIMEOUT_S = 300.0
MAX_ATTEMPTS = 3
_SECRET_FILE = re.compile(r"(?i)(^\.env|\.env$|\.pem$|\.key$|\.p12$|\.pfx$|\.kdbx$|\.keystore$|\.jks$|id_rsa|id_ed25519|"
                          r"secret|credential|password|token|\.npmrc$|\.netrc$|\.pypirc$|\.htpasswd$)")
_SOURCE_EXT = (".py", ".ps1", ".js", ".mjs", ".ts", ".sh", ".bat", ".rs", ".go", ".toml")

PROMPT = """You help describe a software project that AgenticBotPlatform (ABP) runs as a module. Propose better wording
for its module description, the summaries of its operations and the typing of the inputs of the operations people use
most. You can change nothing else: no command, script, route or new input.

Answer ONLY with JSON: an object {{"proposals": [P, ...]}} with exactly {n} proposal(s). Each P:
{{"description": "<one or two sentences, at most {md} characters>",
 "operations": [{{"id": "<an operation id from the list below>", "summary": "<one line, at most {ms} characters>",
   "inputs": [{{"name": "<an existing input of that operation>", "type": "string|integer|number|boolean",
                "required": true|false, "help": "<what it is>", "choices": [<optional list of allowed values>]}}]}}]}}
"inputs" may be left out. Only name inputs that are listed for the operation. {direction}

Project: {name}
Current description: {current}

README:
{readme}

Module manifest:
{manifest}

Operations (id, kind, how it runs, current summary, existing inputs):
{ops}

Source files those operations run:
{files}
"""
PANEL_PROMPT = """You lay out a control panel for a software project that AgenticBotPlatform (ABP) runs as a module. A
fixed renderer draws the panel; you only choose which of the project's operations to feature, how to group them into
sections, their labels and how each result is shown. You can change nothing else: no command, script, route, input or
new operation, and no HTML.

Answer ONLY with JSON: an object {{"proposals": [P, ...]}} with exactly {n} proposal(s). Each P:
{{"panel": {{"version": 1, "title": "<at most {mt} characters>",
   "sections": [{{"title": "<at most {mst} characters>", "note": "<optional, at most {mn} characters>",
     "operations": [{{"id": "<an operation id from the list below>", "label": "<optional, at most {ml} characters>",
                      "output": "table|text|json"}}]}}]}}}}
At most {ms_} sections, {mo} operations per section, each operation at most once. "output" is how the result is shown:
"table" for a list of records, "text" for plain output, "json" otherwise. Feature the operations people use most first.
{direction}

Project: {name}
Description: {current}

Operations (id, kind, how it runs, summary, inputs):
{ops}
"""
DIRECTIONS = ("", "Be concise.", "Be concrete: say what each operation does to the user's files or data.",
              "Write for someone who has never seen the project.", "Prefer plain words over jargon.")


# ---- what the model is shown ---------------------------------------------------------------------------------------

def _get(mid: str):
    try:
        return registry.get(mid)
    except KeyError as e:
        raise ModuleError(str(e.args[0]) if e.args else f"no module {mid!r}", code="not_found", status=404) from None


def _scrub(text: str) -> str:
    from bot.diagnostics import redact
    return redact(text)


def _read(p: Path, cap: int) -> str:
    try:
        return _scrub(p.read_text(encoding="utf-8", errors="replace")[:cap])
    except OSError:
        return ""


def _readme(d: Path) -> str:
    for n in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
        if (d / n).is_file():
            return _read(d / n, MAX_README)
    return "(none)"


def _op_files(d: Path, spec) -> list[Path]:
    """Source files the operations run (files named in argv, in operation order): a few key files."""
    out: list[Path] = []
    root = d.resolve()
    for o in spec.ops:
        for a in o.argv:
            rel = a.replace("{project}/", "").replace("{project}\\", "").replace("\\", "/")
            if "{" in rel or not rel.lower().endswith(_SOURCE_EXT) or rel.startswith(("-", "/")) or ":" in rel \
                    or ".." in rel.split("/"):
                continue
            p = d / rel
            try:
                ok = p.is_file() and p.resolve().is_relative_to(root)
            except OSError:
                ok = False
            if ok and not _SECRET_FILE.search(p.name) and p not in out:
                out.append(p)
        if len(out) >= MAX_FILES:
            break
    return out[:MAX_FILES]


def _spec_path(m) -> tuple[Path, Path]:
    """(the module files' folder, the checkout)."""
    from bot.modules import refresh
    d, overlay = refresh._target(m)
    home = overlay or d
    if not (home / "abp-ops.toml").is_file():
        raise ModuleError(f"{m.name} has no abp-ops.toml to describe", code="unsupported", status=400)
    return home, d


def _load(home: Path):
    from abp_modkit import spec as sp
    return sp.load(home / "abp-ops.toml")


def _op_line(o) -> str:
    how = f"{o.method} {o.path}" if o.kind == "http" else " ".join(o.argv)
    ins = "; ".join(f"{k} ({v.get('type', 'string')}{', required' if v.get('required') else ''})"
                    for k, v in o.inputs.items()) if o.kind == "cmd" else ""
    return f"- {o.id} | {o.kind} | {how[:160]} | {o.summary[:120]!r} | inputs: {ins or 'none'}"


def build_panel_prompt(m, spec, n: int, direction: str = "") -> str:
    from bot.modules import panel as pn
    return PANEL_PROMPT.format(
        n=n, mt=pn.MAX_TITLE, mst=pn.MAX_SECTION_TITLE, mn=pn.MAX_NOTE, ml=pn.MAX_LABEL, ms_=pn.MAX_SECTIONS,
        mo=pn.MAX_OPS_PER_SECTION, direction=direction, name=m.name,
        current=_scrub(m.description or spec.service.description or "(none)"),
        ops="\n".join(_scrub(_op_line(o)) for o in spec.ops[:MAX_OPS_IN_PROMPT]) or "(none)")


def build_prompt(m, home: Path, d: Path, spec, n: int, direction: str = "") -> str:
    files = _op_files(d, spec)
    man = home / "abp-module.toml"
    return PROMPT.format(
        n=n, md=MAX_DESCRIPTION, ms=MAX_SUMMARY, direction=direction, name=m.name,
        current=_scrub(m.description or spec.service.description or "(none)"),
        readme=_readme(d), manifest=_read(man, MAX_MANIFEST) if man.is_file() else "(none)",
        ops="\n".join(_scrub(_op_line(o)) for o in spec.ops[:MAX_OPS_IN_PROMPT]) or "(none)",
        files="\n\n".join(f"--- {p.relative_to(d).as_posix()} ---\n{_read(p, MAX_FILE)}" for p in files) or "(none)")


# ---- checking a proposal -------------------------------------------------------------------------------------------

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


def _choice_ok(t: str, v: Any) -> bool:
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, str) and 0 < len(v) <= 120


def validate_proposal(p: Any, spec) -> tuple[Optional[dict], list[str]]:
    """(the cleaned proposal, problems). Problems empty = acceptable. Only descriptive fields survive."""
    problems: list[str] = []
    if not _keys(p, {"description", "operations", "panel"}, "proposal", problems):
        return None, problems
    out: dict[str, Any] = {"description": "", "operations": []}
    if "description" in p and p["description"] != "":
        out["description"] = _text(p["description"], MAX_DESCRIPTION, "description", problems)
    ops = p.get("operations", [])
    if not isinstance(ops, list):
        return None, problems + ["operations: a list expected"]
    by = {o.id: o for o in spec.ops}
    if "panel" in p:
        from bot.modules import panel as pn
        clean_panel, pp = pn.validate_spec(p["panel"], by)
        problems += [f"panel: {x}" for x in pp]
        if clean_panel is not None:
            out["panel"] = clean_panel
    seen: set[str] = set()
    for i, e in enumerate(ops):
        where = f"operations[{i}]"
        if not _keys(e, {"id", "summary", "inputs"}, where, problems):
            continue
        oid = e.get("id")
        if not isinstance(oid, str) or oid not in by:
            problems.append(f"{where}: {oid!r} is not an operation of this module (no new operations)")
            continue
        if oid in seen:
            problems.append(f"{where}: {oid} appears twice")
            continue
        seen.add(oid)
        op = by[oid]
        rec: dict[str, Any] = {"id": oid}
        if "summary" in e:
            rec["summary"] = _text(e["summary"], MAX_SUMMARY, f"{oid} summary", problems)
        ins = e.get("inputs", [])
        if not isinstance(ins, list):
            problems.append(f"{oid}: inputs: a list expected")
            ins = []
        clean_ins = []
        names: set[str] = set()
        for j, x in enumerate(ins):
            w = f"{oid} inputs[{j}]"
            if not _keys(x, {"name", "type", "required", "help", "choices"}, w, problems):
                continue
            name = x.get("name")
            if op.kind != "cmd" or not isinstance(name, str) or name not in op.inputs:
                problems.append(f"{w}: {name!r} is not an existing input of {oid} (inputs are never added)")
                continue
            if name in names:
                problems.append(f"{w}: {name} appears twice")
                continue
            names.add(name)
            cur = op.inputs[name]
            t = x.get("type", cur.get("type") or "string")
            if t not in INPUT_TYPES:
                problems.append(f"{w}: type must be one of {', '.join(INPUT_TYPES)}")
                continue
            if "required" in x and not isinstance(x["required"], bool):
                problems.append(f"{w}: required must be true or false")
                continue
            rec_in: dict[str, Any] = {"name": name, "type": t}
            if "required" in x:
                rec_in["required"] = x["required"]
            if "help" in x:
                rec_in["help"] = _text(x["help"], MAX_HELP, f"{w} help", problems)
            ch = x.get("choices")
            if ch is not None:
                if (not isinstance(ch, list) or not ch or len(ch) > MAX_CHOICES or
                        not all(_choice_ok(t, c) for c in ch)):
                    problems.append(f"{w}: choices must be 1-{MAX_CHOICES} values of type {t}")
                    continue
                if "default" in cur and cur["default"] not in ch:
                    problems.append(f"{w}: the current default {cur['default']!r} is not among the choices")
                    continue
                rec_in["choices"] = ch
            clean_ins.append(rec_in)
        if clean_ins:
            rec["inputs"] = clean_ins
        out["operations"].append(rec)
    if not out["description"] and not out["operations"] and not out.get("panel") and not problems:
        problems.append("the proposal changes nothing")
    return (out if not problems else None), problems


def parse_answer(raw: str) -> list[Any]:
    """The proposals in a model's answer: JSON, possibly inside a code fence or after some words."""
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    for cand in (s, s[s.find("{"):s.rfind("}") + 1] if "{" in s else ""):
        try:
            v = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(v, dict) and isinstance(v.get("proposals"), list):
            return v["proposals"]
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            return [v]
    raise ValueError("the answer is not JSON with a proposals list")


# ---- the difference against what is there -------------------------------------------------------------------------

def diff(m, spec, prop: dict) -> list[dict]:
    """Rows {target, field, from, to} for everything the proposal would change."""
    rows = []
    cur = m.description or spec.service.description
    if prop["description"] and prop["description"] != cur:
        rows.append({"target": "module", "field": "description", "from": cur, "to": prop["description"]})
    by = {o.id: o for o in spec.ops}
    if prop.get("panel"):
        from bot.modules import panel as pn
        for line in pn.describe(prop["panel"]):
            rows.append({"target": "panel", "field": "section", "from": None, "to": line})
    for e in prop["operations"]:
        o = by[e["id"]]
        if "summary" in e and e["summary"] != o.summary:
            rows.append({"target": e["id"], "field": "summary", "from": o.summary, "to": e["summary"]})
        for x in e.get("inputs", []):
            now = o.inputs[x["name"]]
            for f, key in (("type", "type"), ("required", "required"), ("help", "description"), ("choices", "enum")):
                if f in x:
                    was = now.get(key, "string" if f == "type" else False if f == "required" else None)
                    if x[f] != was:
                        rows.append({"target": f"{e['id']} / {x['name']}", "field": f, "from": was, "to": x[f]})
    return rows


# ---- storing proposals ---------------------------------------------------------------------------------------------

def _dir(mid: str) -> Path:
    from bot.studio.variants import studio_dir
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", mid):
        raise ModuleError("bad module id", code="invalid", status=400)
    d = studio_dir() / "assist" / mid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fingerprint(home: Path) -> str:
    h = hashlib.sha256()
    for f in ("abp-ops.toml", "abp-module.toml"):
        p = home / f
        h.update(p.read_bytes() if p.is_file() else b"-")
    return h.hexdigest()[:16]


def _batch_path(mid: str, batch: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{6,32}", batch or ""):
        raise ModuleError("bad batch id", code="invalid", status=400)
    return _dir(mid) / f"{batch}.json"


def batches(mid: str, *, limit: int = 5) -> list[dict]:
    """Stored batches of a module, newest first, each with its proposals and their differences (as made)."""
    rows = []
    for p in sorted(_dir(mid).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return rows


def proposals(mid: str) -> dict:
    """The latest batches, each flagged whether it is still current (the module files did not change since)."""
    m = _get(mid)
    home, _d = _spec_path(m)
    rows = batches(mid)
    fp = _fingerprint(home)
    for b in rows:
        b["current"] = b.get("fingerprint") == fp
    return {"id": m.id, "batches": rows}


# ---- generating ----------------------------------------------------------------------------------------------------

async def _ask(chain: list, prompt: str, group: str, mid: str) -> tuple[Optional[str], str, list[str]]:
    from bot.agent_runtime import moa
    from bot.studio import log
    failures: list[str] = []
    for provider, model in chain[:MAX_ATTEMPTS]:
        try:
            raw = await asyncio.wait_for(moa._single_call(provider, model, prompt, max_tokens=6000,
                                                          timeout_s=PER_CALL_TIMEOUT_S), timeout=PER_CALL_TIMEOUT_S + 30)
            return raw, f"{provider}/{model}", failures
        except Exception as e:  # noqa: BLE001 - a rate limit or a timeout: the next model takes over
            failures.append(f"{provider}/{model}: {str(e)[:300]}")
            log.event("assist-failed", group=group, module=mid, model=f"{provider}/{model}", error=str(e)[:500])
    return None, "", failures


async def generate(mid: str, *, n: int = 3, models: Optional[list[str]] = None, panel: bool = False) -> dict:
    """Ask for up to `n` proposals and store the batch. Writes nothing into the module. `panel`: layouts for the
    module's panel only (bot.modules.panel), not wording."""
    from bot.studio import generate as gen
    from bot.studio import log, variants
    m = _get(mid)
    home, d = await asyncio.to_thread(_spec_path, m)
    spec = await asyncio.to_thread(_load, home)
    n = max(1, min(int(n), MAX_PROPOSALS))
    try:
        cands = await gen.candidates(models)
    except variants.StudioError as e:
        raise ModuleError(str(e), code="no_model", status=409) from None
    group = uuid.uuid4().hex[:12]
    if panel:
        prompts = [build_panel_prompt(m, spec, 1, DIRECTIONS[i % len(DIRECTIONS)]) for i in range(n)]
    else:
        prompts = [await asyncio.to_thread(build_prompt, m, home, d, spec, 1, DIRECTIONS[i % len(DIRECTIONS)])
                   for i in range(n)]
    chains = [[cands[(i + k) % len(cands)] for k in range(len(cands))] for i in range(n)]
    answers = await asyncio.gather(*(_ask(c, p, group, mid) for c, p in zip(chains, prompts)))
    out, errors = [], []
    for raw, model, failures in answers:
        if raw is None:
            errors.append("every model failed: " + " | ".join(failures)[:600])
            continue
        try:
            items = parse_answer(raw)
        except ValueError as e:
            out.append({"model": model, "ok": False, "problems": [str(e)], "proposal": None, "diff": []})
            continue
        for item in items[:1]:                      # each call was asked for one: more is ignored
            clean, problems = validate_proposal(item, spec)
            rec = {"model": model, "ok": clean is not None, "problems": problems, "proposal": clean,
                   "diff": diff(m, spec, clean) if clean else []}
            if clean is not None and not rec["diff"]:
                rec.update(ok=False, problems=["the proposal changes nothing that is not already so"])
            out.append(rec)
    for i, rec in enumerate(out, 1):
        rec["n"] = i
    batch = {"batch": group, "module": m.id, "kind": "panel" if panel else "setup", "made": round(time.time(), 1), "fingerprint": _fingerprint(home),
             "proposals": out, "errors": errors}
    (_dir(m.id) / f"{group}.json").write_text(json.dumps(batch, indent=1, default=str), encoding="utf-8")
    log.event("assist", module=m.id, batch=group, proposals=len(out), valid=sum(1 for r in out if r["ok"]),
              errors=errors)
    return {**batch, "current": True, "wrote": []}


# ---- keeping one ---------------------------------------------------------------------------------------------------

def comment_lines(text: str) -> int:
    """How many lines after the first carry a TOML comment (whole-line or inline; a # inside a string is not one)."""
    n, ml = 0, ""                                   # ml: the delimiter of an open multi-line string
    for line in text.splitlines()[1:]:
        i, found = 0, False
        while i < len(line):
            if ml:
                j = line.find(ml, i)
                if j < 0:
                    break
                i, ml = j + 3, ""
                continue
            c = line[i]
            if c == "#":
                found = True
                break
            if line.startswith(('"""', "'''"), i):
                ml, i = line[i:i + 3], i + 3
            elif c == '"':
                i += 1
                while i < len(line) and line[i] != '"':
                    i += 2 if line[i] == chr(92) else 1
                i += 1
            elif c == "'":
                j = line.find("'", i + 1)
                i = len(line) if j < 0 else j + 1
            else:
                i += 1
        n += found
    return n


def keep(mid: str, batch: str, number: int, *, drop_comments: bool = False) -> dict:
    """Write the chosen proposal as hand edits: summaries, input typing and the description go into abp-ops.toml (and
    the description into a generated manifest), which adopt/refresh keep. Refused for an invalid proposal or when the
    module files changed since the batch was made. A hand-owned abp-ops.toml is written again from its parsed form,
    which loses its comments (all but the first line): that is refused unless `drop_comments` is given."""
    from abp_modkit import adopt as ad
    from abp_modkit import spec as sp
    from bot.studio import log
    m = _get(mid)
    home, _d = _spec_path(m)
    p = _batch_path(m.id, batch)
    if not p.is_file():
        raise ModuleError(f"no such batch: {batch}", code="not_found", status=404)
    b = json.loads(p.read_text(encoding="utf-8"))
    rec = next((r for r in b["proposals"] if r.get("n") == number), None)
    if rec is None:
        raise ModuleError(f"the batch has no proposal {number}", code="not_found", status=404)
    spec = _load(home)
    prop, problems = validate_proposal(rec.get("proposal"), spec) if rec.get("ok") else (None, rec.get("problems") or [])
    if prop is None:
        raise ModuleError("that proposal was rejected and cannot be kept: " + "; ".join(problems), code="rejected",
                          status=409)
    if b.get("fingerprint") != _fingerprint(home):
        raise ModuleError("the module's files changed since these proposals were made; make new ones", code="stale",
                          status=409)
    if not prop["description"] and not prop["operations"]:           # a panel layout only: abp-ops.toml stays as it is
        from bot.modules import panel as pn
        wrote = [pn.write(home, prop["panel"])]
        log.event("assist-keep", module=m.id, batch=batch, proposal=number, model=rec.get("model"), wrote=wrote,
                  changes=len(rec.get("diff") or []))
        return {"id": m.id, "kept": number, "batch": batch, "wrote": wrote, "changes": rec.get("diff") or []}
    ops_path, man_path = home / ad.OPS_FILE, home / ad.MANIFEST_FILE
    first = (ops_path.read_text(encoding="utf-8").splitlines() or [""])[0]
    if prop["description"]:
        spec.service.description = prop["description"]
    by = {o.id: o for o in spec.ops}
    for e in prop["operations"]:
        o = by[e["id"]]
        if "summary" in e:
            o.summary = e["summary"]
        for x in e.get("inputs", []):
            cur = o.inputs[x["name"]]
            cur["type"] = x["type"]
            if "required" in x:
                cur["required"] = x["required"]
            if "help" in x:
                cur["description"] = x["help"]
            if "choices" in x:
                cur["enum"] = x["choices"]
    lost = comment_lines(ops_path.read_text(encoding="utf-8")) if ad.OPS_MARKER not in first else 0
    if lost and not drop_comments:
        raise ModuleError(f"abp-ops.toml is hand-owned and has {lost} comment line(s) that writing this proposal would "
                          "lose (the first line stays). Nothing was written; keep it again with drop_comments to accept that",
                          code="comments", status=409)
    text = sp.dump(spec, header=first if first.startswith("#") else "")
    sp.parse(tomllib.loads(text), str(ops_path))                   # what we write must load
    ops_path.write_text(text, encoding="utf-8", newline="\n")
    wrote = [ad.OPS_FILE]
    if prop["description"] and man_path.is_file():
        mt = man_path.read_text(encoding="utf-8")
        if ad.MARKER in (mt.splitlines() or [""])[0]:
            new = re.sub(r"(?m)^description = .*$", lambda _m: f"description = {sp.q(prop['description'])}", mt, count=1)
            if new != mt:
                man_path.write_text(new, encoding="utf-8", newline="\n")
                wrote.append(ad.MANIFEST_FILE)
    if prop.get("panel"):
        from bot.modules import panel as pn
        wrote.append(pn.write(home, prop["panel"]))
    registry.modules(refresh=True)
    log.event("assist-keep", module=m.id, batch=batch, proposal=number, model=rec.get("model"), wrote=wrote,
              changes=len(rec.get("diff") or []))
    return {"id": m.id, "kept": number, "batch": batch, "wrote": wrote, "changes": rec.get("diff") or []}

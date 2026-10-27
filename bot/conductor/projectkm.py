"""Project knowledge modules (LF7): a repository's knowledge as ModelMistress `mmkm/1` KMs, retrieved into local prompts by task.

    build(root, repo, out_dir)     two KMs for a repository, written as mmkm/1 directories (km.toml + hashed payload):
                                     project.<repo>.knowledge   kind "memory": conventions and rules (AGENTS.md / CLAUDE.md /
                                                                CONTRIBUTING.md sections), module contracts (each module's
                                                                docstring head), and, when `lessons` is on, review findings and
                                                                common fixes from earlier Factory runs (bot/conductor/knowledge.py)
                                     project.<repo>.lessons     kind "data": rejected attempts paired with the accepted fix, as
                                                                mmkm/1 training records (status "pending"; GEN-D feeds them on)
    load(km_dir)                   the memory KM's chunks, after checking every payload hash (a tampered KM is refused)
    select(chunks, text, files)    the chunks for one task: path matches first, then word overlap, within a character budget
    km_block(...)                  build-or-reuse + select + format: what the planner and a worker are given

A KM version is immutable: a rebuild whose content changed writes the next patch version with `parent` set to the one before;
an unchanged rebuild reuses the current one. Data only: nothing in a KM executes. No embeddings yet (the spec lets the vector
term drop when there is no centroid); selection is lexical and explainable.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("bot.conductor.projectkm")

SPEC = "mmkm/1"
RULE_FILES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md")
SOURCE_DIRS_SKIP = {".git", ".venv", "venv", "node_modules", "target", "dist", "build", "__pycache__", ".claude", "tests", "sdk"}
MAX_MODULES = 2000
_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")
_STOP = frozenset("the and for with that this from into its are not can will one all any each when then than use used uses "
                  "new file files code make makes should must have has was were been also only what which where".split())


def _stem(w: str) -> str:
    """A crude plural fold (flags -> flag, processes -> process), so a task and a rule that say the same thing match."""
    if w.endswith("sses") or w.endswith("ies"):
        return w[:-2]
    return w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 4 else w


def words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(str(text or "").lower().replace("/", " ").replace(".", " ")) if w not in _STOP}


# ------------------------------------------------------------------ sources
def rule_chunks(root: Path) -> list[dict]:
    """One chunk per `##` section of the repo's rule files (conventions, critical rules, the repo map)."""
    out = []
    for name in RULE_FILES:
        p = Path(root) / name
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for sec in re.split(r"(?m)^(?=## )", text):
            sec = sec.strip()
            if len(sec) < 40:
                continue
            title = sec.splitlines()[0].lstrip("# ").strip()
            # one chunk per list item or paragraph, so a task gets the one rule it needs, not the whole section
            for item in re.split(r"\n(?=\s*(?:[-*]|\d+\.)\s)|\n\s*\n", "\n".join(sec.splitlines()[1:])):
                item = " ".join(item.split())
                if len(item) >= 30 and not item.startswith(("|", "```")):
                    out.append({"kind": "convention", "source": name, "title": title, "text": f"({title}) {item[:700]}",
                                "paths": re.findall(r"`([\w./-]+\.\w+)`", item)})
    return out


def _module_name(rel: str) -> str:
    return rel[:-3].replace("/", ".").removesuffix(".__init__")


def module_chunks(root: Path, limit: int = MAX_MODULES) -> list[dict]:
    """Each Python module's contract: the first paragraph of its docstring (what it is for, what it promises)."""
    root = Path(root)
    out = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        if set(rel.split("/")[:-1]) & SOURCE_DIRS_SKIP or rel.startswith("test"):
            continue
        try:
            doc = ast.get_docstring(ast.parse(p.read_text(encoding="utf-8")), clean=True) or ""
        except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
            continue
        head = doc.split("\n\n")[0].strip()
        if len(head) < 20:
            continue
        out.append({"kind": "contract", "source": rel, "title": _module_name(rel), "text": f"{rel}: {' '.join(head.split())[:500]}", "paths": [rel]})
        if len(out) >= limit:
            break
    return out


def lesson_chunks(repo: str) -> list[dict]:
    """Review findings and common fixes from earlier Factory runs: the rules that keep failing, and each rejection's words."""
    try:
        from bot.conductor import knowledge
    except Exception:  # noqa: BLE001
        return []
    rows = knowledge.lessons(repo, limit=400)
    out = [{"kind": "review_rule", "source": "factory", "title": f"review rule {b['rule']}",
            "text": f"Factory reviews rejected {b['count']} tasks for the rule '{b['rule']}'. Respect it.", "paths": []}
           for b in knowledge.failing_rules(rows)]
    for r in rows:
        if r["outcome"] != "accepted" and r["text"]:
            out.append({"kind": "finding", "source": "factory", "title": r["title"][:120],
                        "text": f"Earlier task '{r['title']}' was {r['outcome']}: {' '.join(r['text'].split())[:600]}", "paths": r["files"][:6]})
    return out


def data_records(repo: str) -> list[dict]:
    """mmkm/1 §8 training records: a rejected attempt and, when the same task later passed, the accepted outcome."""
    try:
        from bot.conductor import knowledge
    except Exception:  # noqa: BLE001
        return []
    rows = sorted(knowledge.lessons(repo, limit=5000), key=lambda r: r["id"])
    fixed = {(r["plan_id"], r["task_key"]): r for r in rows if r["outcome"] == "accepted"}
    out = []
    for r in rows:
        if r["outcome"] == "accepted":
            continue
        ok = fixed.get((r["plan_id"], r["task_key"]))
        out.append({"id": f"r-{r['id']:05d}", "task": "code_review_fix",
                    "input": [{"role": "user", "content": f"Task: {r['title']}\nFiles: {', '.join(r['files'])}\nReviewer: {r['text']}"}],
                    "output": ok["text"] if ok else "", "labels": {"outcome": r["outcome"], "rules": r["rules"]},
                    "tags": ["task:codegen", "source:factory"], "source": {"kind": "review", "trace": r["plan_id"]},
                    "split": "train", "status": "pending", "rev": 1})
    return out


# ------------------------------------------------------------------ write / read
def _toml_str(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_str(x) for x in v) + "]"
    return json.dumps(str(v))                  # a JSON string is a valid TOML basic string


def _manifest(meta: dict, tables: dict[str, dict], payload: list[dict]) -> str:
    lines = [f"{k} = {_toml_str(v)}" for k, v in meta.items() if v not in (None, "")]
    for name, tab in tables.items():
        lines += ["", f"[{name}]"] + [f"{k} = {_toml_str(v)}" for k, v in tab.items()]
    for p in payload:
        lines += ["", "[[payload]]"] + [f"{k} = {_toml_str(v)}" for k, v in p.items()]
    return "\n".join(lines) + "\n"


def _versions(out_dir: Path, km_id: str) -> list[tuple[tuple[int, ...], Path]]:
    found = []
    for d in Path(out_dir).glob(f"{km_id}@*"):
        try:
            found.append((tuple(int(x) for x in d.name.split("@", 1)[1].split(".")), d))
        except ValueError:
            continue
    return sorted(found)


def _payload_digest(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    return h.hexdigest()


def write_km(out_dir: Path, km_id: str, kind: str, name: str, description: str, files: dict[str, bytes], *,
             tables: Optional[dict[str, dict]] = None, tags: Iterable[str] = (), descriptor: str = "") -> Path:
    """Write one KM version; reuse the latest when its payload is identical (versions are immutable)."""
    out_dir = Path(out_dir)
    digest = _payload_digest(files)
    have = _versions(out_dir, km_id)
    if have:
        latest = have[-1][1]
        try:
            if json.loads((latest / "payload" / ".digest.json").read_text(encoding="utf-8")).get("digest") == digest:
                return latest
        except (OSError, ValueError):
            pass
    ver = have[-1][0] if have else None
    version = f"{ver[0]}.{ver[1]}.{ver[2] + 1}" if ver and len(ver) == 3 else "1.0.0"
    km_uuid = ""
    if have:
        m = re.search(r'(?m)^uuid = "([^"]+)"', (have[-1][1] / "km.toml").read_text(encoding="utf-8"))
        km_uuid = m.group(1) if m else ""
    d = out_dir / f"{km_id}@{version}"
    (d / "payload").mkdir(parents=True, exist_ok=True)
    payload = []
    for rel, data in sorted(files.items()):
        (d / rel).write_bytes(data)
        payload.append({"path": rel, "role": rel.split("/")[-1].split(".")[0], "format": rel.rsplit(".", 1)[-1], "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    (d / "payload" / ".digest.json").write_text(json.dumps({"digest": digest}), encoding="utf-8")
    meta = {"spec": SPEC, "id": km_id, "version": version, "uuid": km_uuid or str(uuid.uuid4()), "kind": kind, "name": name,
            "description": description, "domain": "project/" + km_id.split(".")[1], "tags": sorted(set(tags)),
            "license": "", "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "parent": f"{km_id}@{'.'.join(map(str, ver))}" if ver else ""}
    tabs = {"compat": {"base": "any"}, "modalities": {"input": ["text"], "output": ["text"]},
            "routing": {"descriptor": descriptor[:400], "embed_model": "", "keywords": [], "intents": ["write_code", "fix_bug"],
                        "prior": 0.0},
            "provenance": {"created_by": "auto", "teachers": [], "data": []}, **(tables or {})}
    (d / "km.toml").write_text(_manifest(meta, tabs, payload), encoding="utf-8")
    return d


def build(root: Path, repo: str, out_dir: Path, *, lessons: bool = True) -> dict:
    """Build (or reuse) the repository's memory KM and, when there are lessons, its data KM."""
    chunks = rule_chunks(root) + module_chunks(root) + (lesson_chunks(repo) if lessons else [])
    for i, c in enumerate(chunks):
        c["id"] = f"c{i:05d}"
    body = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks).encode("utf-8")
    slug = re.sub(r"[^a-z0-9-]+", "-", repo.lower()).strip("-") or "repo"
    mem = write_km(out_dir, f"project.{slug}.knowledge", "memory", f"{repo} project knowledge",
                   f"Conventions, owner rules, module contracts and review findings for {repo}.",
                   {"payload/chunks.jsonl": body},
                   tables={"memory": {"chunks": "payload/chunks.jsonl", "embeddings": "", "dim": 0, "index": "flat"}},
                   tags=["task:codegen", "modality:text", f"project:{slug}"],
                   descriptor=" ".join(sorted({c["title"] for c in chunks if c["kind"] == "convention"})))
    out = {"memory": str(mem), "chunks": len(chunks),
           "by_kind": {k: sum(1 for c in chunks if c["kind"] == k) for k in sorted({c["kind"] for c in chunks})}}
    recs = data_records(repo) if lessons else []
    if recs:
        data = write_km(out_dir, f"project.{slug}.lessons", "data", f"{repo} Factory lessons",
                        f"Rejected Factory attempts and their fixes in {repo}, as training records.",
                        {"payload/records.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs).encode("utf-8")},
                        tags=["task:codegen", f"project:{slug}"])
        out.update(data=str(data), records=len(recs))
    return out


class KMInvalid(ValueError):
    pass


def load(km_dir: Path) -> list[dict]:
    """The memory KM's chunks, every payload file checked against the manifest's size and SHA-256 first."""
    km_dir = Path(km_dir)
    try:
        import tomllib

        man = tomllib.loads((km_dir / "km.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KMInvalid(f"unreadable manifest: {exc}") from exc
    if man.get("spec") != SPEC or man.get("kind") != "memory":
        raise KMInvalid("not an mmkm/1 memory KM")
    for p in man.get("payload") or []:
        rel = str(p.get("path") or "")
        if rel.startswith(("/", "\\")) or ".." in rel.replace("\\", "/").split("/"):
            raise KMInvalid(f"payload path not allowed: {rel}")
        data = (km_dir / rel).read_bytes()
        if len(data) != int(p.get("bytes", -1)) or hashlib.sha256(data).hexdigest() != p.get("sha256"):
            raise KMInvalid(f"payload {rel} does not match its hash")
    rel = (man.get("memory") or {}).get("chunks") or "payload/chunks.jsonl"
    return [json.loads(ln) for ln in (km_dir / rel).read_text(encoding="utf-8").splitlines() if ln.strip()]


# ------------------------------------------------------------------ selection
MAX_ITEMS = {"contract": 3, "convention": 3, "finding": 3, "review_rule": 3}


def select(chunks: list[dict], text: str, files: Optional[list] = None, *, budget_chars: int = 2500, strict: bool = True) -> list[dict]:
    """The chunks one task needs, within `budget_chars`.

    strict (the default): a module contract only for a file the task names, or for a module in the same folder whose words
    match the task well; a convention or finding only when it shares at least two words with the task; at most MAX_ITEMS per kind.
    The 2026-10-26/27 bench runs got loosely related module descriptions instead, and they sent the model reading modules
    the task never needed. strict=False keeps the earlier, looser ranking."""
    want, fset = words(text), {str(f).replace("\\", "/") for f in files or []}
    # a sibling is a module in the same sub-package (bot/sandbox_ns/...), never just in the top-level package (bot/)
    fdirs = {f.rsplit("/", 1)[0] for f in fset if f.count("/") >= 2}
    fwords = words(" ".join(fset))
    scored = []
    for c in chunks:
        cw = words(f"{c.get('title', '')} {c.get('text', '')}")
        shared = want & cw
        overlap = len(shared) / (1 + len(cw) ** 0.5)
        paths = c.get("paths") or []
        path_hit = bool(fset & set(paths))
        kind = c["kind"]
        if strict:
            if kind == "contract":
                sibling = any(p.rsplit("/", 1)[0] in fdirs for p in paths if "/" in p)
                if not path_hit and not (sibling and overlap >= 0.6):
                    continue
            elif kind == "convention" and not path_hit and len(shared) < 2:
                continue
            elif kind in ("finding", "review_rule") and not path_hit and len(shared) < 2:
                continue
        near = bool(fwords & words(" ".join(paths))) and kind == "contract"
        score = (3.0 if path_hit else 0.0) + (0.6 if near else 0.0) + overlap + (0.5 if kind in ("review_rule", "finding") else 0.0)
        if kind == "convention":
            score += 0.3                     # the owner's rules beat a module's description at equal overlap
        if score > 0.35:
            scored.append((score, c))
    scored.sort(key=lambda sc: -sc[0])
    out, used, per = [], 0, {}
    for _s, c in scored:
        if used + len(c["text"]) > budget_chars or (strict and per.get(c["kind"], 0) >= MAX_ITEMS.get(c["kind"], 3)):
            continue
        out.append(c)
        used += len(c["text"])
        per[c["kind"]] = per.get(c["kind"], 0) + 1
    return out


def format_block(chosen: list[dict]) -> str:
    """Framed as reference: phrased as "follow these" and placed after the task, it read as more instructions."""
    if not chosen:
        return ""
    return ("Background from the project's knowledge module (reference only, not part of the task; use what fits and ignore "
            "the rest):\n") + "\n".join(f"- {c['text'].strip()}" for c in chosen)


_cache: dict[str, tuple[float, list[dict]]] = {}


def km_dir() -> Path:
    from bot.agent_runtime.state import state_dir

    return state_dir("conductor", "km")


def km_block(root: Path, repo: str, text: str, files: Optional[list] = None, *, budget_chars: int = 2500, lessons: bool = True,
             out_dir: Optional[Path] = None, max_age_s: float = 600) -> str:
    """Build (at most every `max_age_s` per repo) or reuse the KM, then select and format the chunks for one task."""
    try:
        key = f"{Path(root).resolve()}|{repo}|{lessons}|{out_dir or ''}"
        hit = _cache.get(key)
        if hit is None or time.time() - hit[0] > max_age_s:
            info = build(Path(root), repo, Path(out_dir) if out_dir else km_dir(), lessons=lessons)
            hit = (time.time(), load(Path(info["memory"])))
            _cache[key] = hit
        return format_block(select(hit[1], text, files, budget_chars=budget_chars))
    except Exception as exc:  # noqa: BLE001 - knowledge is an extra, never a reason a run fails
        logger.debug("project KM unavailable: %s", exc)
        return ""

"""Skills as folders: the SKILL.md standard (roadmap P4).

A skill pack is a folder containing `SKILL.md` - YAML front matter with a `name` and a
`description`, then instructions - and, optionally, bundled files (scripts, reference
documents, templates):

    my-skill/
      SKILL.md          ---
                        name: my-skill
                        description: When to use this and what it does (one or two sentences).
                        ---
                        Step-by-step instructions ...
      scripts/build.py
      reference/api.md

**Progressive disclosure.** Only the name and description of each skill go into the system
prompt. The model loads a skill's instructions with `read_skill`, which also lists the bundled
files, and reads one of those with `read_skill_file` only when the instructions point to it.
Nothing is loaded that is not needed.

**Where they are found.** `<ABP data>/skill_packs/<name>/` (yours), and in the working directory
`.claude/skills/`, `.agents/skills/` and `.abp/skills/`. A skill written for another agent product
works as it is. Your own skills beat a repository's of the same name.

**Linked libraries.** `native_agent.skills.external_dirs` lists other folders of skills, read in place:
another agent's library, for example Hermes's or OpenClaw's (abp_import links them). They are
searched up to three folders deep, since some products group skills by category, and hidden folders
are skipped (archives and caches live there). They rank between a repository's skills and your own.

**Large libraries.** A library can hold thousands of skills. Each SKILL.md is parsed once and
reused until the file changes, and a linked folder's listing is refreshed at most every 30 seconds.
The system prompt names at most 40 skills; past that it says how many there are, and the agent
finds the right one with `list_skills` and a `query`.

**What a skill cannot do.** A skill is text. It cannot run anything by itself: a bundled script
runs only if the agent decides to run it with `run_shell`, which asks for approval like any
command. `allowed-tools` in the front matter is shown but grants nothing. Skills that come from a
repository are labelled as such, their descriptions are trimmed to one short line, and a session
that loads one is not treated differently from any other - so read what you install (see
skill_install.py for the quarantine and scan used when fetching one from the internet).
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bot.agent_runtime import toolspec
from bot.agent_runtime.errors import ToolError, safe_path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_SKILL_MD = 40_000
MAX_FILE_READ = 100_000
MAX_LISTED_FILES = 60
DESCRIPTION_SHOWN = 300
SUMMARY_LIMIT = 40
EXTERNAL_DEPTH = 3
LINKED_TTL_S = 60.0
FIRST_LOAD_WAIT_S = 5.0
SKIP = {".git", "__pycache__", "node_modules"}
_parsed: dict[str, tuple[int, int, Optional["Skill"]]] = {}       # "source|SKILL.md" -> (mtime_ns, size, parsed)
_files: dict[str, tuple] = {}                                       # skill folder -> its bundled files
_linked: dict[str, "_Library"] = {}                                 # linked root -> its loaded skills
_linked_lock = threading.Lock()


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    directory: Path
    source: str                       # user | project | linked
    allowed_tools: tuple = ()
    problems: tuple = field(default_factory=tuple)

    @property
    def files(self) -> tuple:
        """Bundled files (relative paths), listed on first use: a large library would otherwise walk every folder."""
        key = str(self.directory)
        if key not in _files:
            found = []
            for p in sorted(self.directory.rglob("*")):
                rel = p.relative_to(self.directory)
                if p.is_file() and p.name != "SKILL.md" and not any(part in SKIP for part in rel.parts):
                    found.append(rel.as_posix())
            _files[key] = tuple(found)
        return _files[key]


def user_root() -> Optional[Path]:
    try:
        from bot import envfile

        home = getattr(envfile, "ABP_HOME_ACTIVE", None)
        return (Path(home) if home else Path(envfile.PROJECT_ROOT) / "data") / "skill_packs"
    except Exception:  # noqa: BLE001
        return None


def external_libraries() -> list[tuple[Path, frozenset]]:
    """Linked skill libraries (native_agent.skills.external_dirs): each a folder, or {path, exclude: [skill names]}."""
    try:
        from bot.config import config

        dirs = (((config.current.get("native_agent") or {}).get("skills")) or {}).get("external_dirs") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for d in dirs:
        path, exclude = (d.get("path"), d.get("exclude") or ()) if isinstance(d, dict) else (d, ())
        if str(path or "").strip():
            out.append((Path(str(path)).expanduser(), frozenset(str(n).lower() for n in exclude)))
    return out


def external_roots() -> list[Path]:
    return [root for root, _ in external_libraries()]


def project_roots(workspace: Optional[Path]) -> list[Path]:
    if workspace is None:
        return []
    return [Path(workspace) / rel for rel in (".claude/skills", ".agents/skills", ".abp/skills")]


def _front_matter(text: str) -> tuple[dict, str, Optional[str]]:
    from bot.agent_runtime.agent_defs import _split_front_matter

    return _split_front_matter(text)


_SIMPLE = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$")


def _quick_front_matter(text: str) -> Optional[tuple[dict, str]]:
    """name / description / allowed-tools from front matter made of plain `key: value` lines, without YAML: parsing
    YAML was most of the cost of loading a large library. None when the block is not that simple (a folded or
    quoted-multiline value, say); the caller then uses the full parser."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    meta: dict = {}
    wanted = ("name", "description", "allowed-tools", "allowed_tools")
    last = ""
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t-":
            if last in wanted:
                return None                            # a value we read continues on the next line: let YAML join it
            continue                                   # nested metadata we do not read
        m = _SIMPLE.match(line)
        if not m:
            return None
        key, value = m.group(1), m.group(2).strip()
        last = key
        if key in ("name", "description", "allowed-tools", "allowed_tools"):
            if value[:1] in ("|", ">", "[", "{", "&", "*", "!") or (value[:1] in "\"'" and not value.endswith(value[:1])):
                return None
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1]
            meta[key] = value
    body_start = text.find("\n", end + 4)
    return meta, (text[body_start + 1:] if body_start >= 0 else "")


def load_dir(directory: Path, source: str) -> Optional[Skill]:
    """The skill in `directory`, parsed once and reused until its SKILL.md changes."""
    md = directory / "SKILL.md"
    try:
        st = md.stat()
    except OSError:
        return None
    key = f"{source}|{md}"
    hit = _parsed.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    skill = _parse_dir(directory, md, st.st_size, source)
    _parsed[key] = (st.st_mtime_ns, st.st_size, skill)
    _files.pop(str(directory), None)
    return skill


def _parse_dir(directory: Path, md: Path, size: int, source: str) -> Optional[Skill]:
    try:
        if size > MAX_SKILL_MD * 4:
            return None
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    quick = _quick_front_matter(text)
    if quick is not None:
        (meta, body), problem = quick, None
    else:
        meta, body, problem = _front_matter(text)
    problems = [problem] if problem else []
    name = str(meta.get("name") or directory.name).strip().lower()
    if not NAME_RE.match(name):
        problems.append(f"the name {name!r} must be lowercase letters, digits and hyphens")
        return None
    if name != directory.name.lower():
        problems.append(f"the name {name!r} does not match its folder {directory.name!r}")
    description = " ".join(str(meta.get("description") or "").split())
    if not description:
        problems.append("no description, so the model cannot tell when to use it")
    tools_field = meta.get("allowed-tools") or meta.get("allowed_tools") or ()
    if isinstance(tools_field, str):
        tools_field = [t for t in re.split(r"[,\s]+", tools_field) if t]
    return Skill(name, description[:1024], body.strip()[:MAX_SKILL_MD], directory, source,
                 tuple(str(t) for t in tools_field), tuple(problems))


def _scan(root: Path, source: str, into: dict[str, Skill]) -> None:
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return
    for d in entries:
        s = load_dir(d, source)
        if s is not None:
            into[s.name] = s


def _skill_folders(root: Path, depth: int) -> list[Path]:
    """Folders holding a SKILL.md under `root`, up to `depth` levels down; hidden folders are skipped."""
    out: list[Path] = []
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name not in SKIP)
    except OSError:
        return out
    for d in children:
        if (d / "SKILL.md").is_file():
            out.append(d)
        elif depth > 1:
            out.extend(_skill_folders(d, depth - 1))
    return out


class _Library:
    """One linked folder's skills. Loaded and refreshed on a background thread, so no turn waits for a large library
    (the first load waits briefly, then the turn goes ahead with whatever is ready)."""

    def __init__(self, root: Path):
        self.root = root
        self.skills: dict[str, Skill] = {}
        self.loaded_at = 0.0
        self.loading = False
        self.ready = threading.Event()

    def _load(self) -> None:
        found: dict[str, Skill] = {}
        try:
            for d in _skill_folders(self.root, EXTERNAL_DEPTH):
                s = load_dir(d, "linked")
                if s is not None and s.name not in found:      # the first of a name wins inside one library
                    found[s.name] = s
        finally:
            self.skills, self.loaded_at, self.loading = found, time.monotonic(), False
            self.ready.set()

    def get(self) -> dict[str, Skill]:
        with _linked_lock:
            stale = not self.ready.is_set() or time.monotonic() - self.loaded_at > LINKED_TTL_S
            start = stale and not self.loading
            if start:
                self.loading = True
        if start:
            threading.Thread(target=self._load, name="abp-skill-library", daemon=True).start()
        if not self.ready.is_set():
            self.ready.wait(FIRST_LOAD_WAIT_S)
        return self.skills


def linked_status() -> list[dict]:
    """Each linked library: how many skills are loaded, and whether its first load is still running."""
    out = []
    for root, _ in external_libraries():
        lib = _linked.get(str(root))
        out.append({"path": str(root), "skills": len(lib.skills) if lib else 0, "loading": bool(lib and not lib.ready.is_set())})
    return out


def _scan_linked(root: Path, into: dict[str, Skill], exclude: frozenset = frozenset()) -> None:
    with _linked_lock:
        lib = _linked.setdefault(str(root), _Library(root))
    for name, s in lib.get().items():
        if name not in exclude and s.directory.name.lower() not in exclude:
            into.setdefault(name, s)


def discover(workspace: Optional[Path] = None) -> dict[str, Skill]:
    """Every skill pack visible from `workspace`; your own win over linked libraries, which win over a repository's."""
    project: dict[str, Skill] = {}
    for root in project_roots(workspace):
        _scan(root, "project", project)
    linked: dict[str, Skill] = {}
    for root, exclude in external_libraries():
        _scan_linked(root, linked, exclude)
    user: dict[str, Skill] = {}
    ur = user_root()
    if ur is not None:
        _scan(ur, "user", user)
    return {**project, **linked, **user}


_WORD = re.compile(r"[a-z0-9]+")


def search(workspace: Optional[Path], query: str, limit: int = 20) -> list[Skill]:
    """Skills whose name or description match the words of `query`, best first."""
    words = [w for w in _WORD.findall((query or "").lower()) if len(w) > 1]
    if not words:
        return []
    scored = []
    for s in discover(workspace).values():
        name, desc = s.name.replace("-", " "), s.description.lower()
        score = sum(3 * (w in name) + (w in desc) for w in words)
        if score:
            scored.append((-score, s.name, s))
    scored.sort(key=lambda t: t[:2])
    return [s for _, _, s in scored[:max(1, limit)]]


def get(workspace: Optional[Path], name: str) -> Optional[Skill]:
    return discover(workspace).get(str(name or "").strip().lower())


def summary(workspace: Optional[Path]) -> str:
    order = {"project": 0, "user": 1, "linked": 2}
    skills = sorted(discover(workspace).values(), key=lambda s: (order.get(s.source, 3), s.name))
    if not skills:
        return ""
    lines = ["Skill packs (folders of instructions; load one with read_skill, and its bundled files with read_skill_file):"]
    for s in skills[:SUMMARY_LIMIT]:
        desc = s.description[:DESCRIPTION_SHOWN] or "(no description)"
        lines.append(f"- {s.name}: {desc}" + (" [from this project]" if s.source == "project" else ""))
    if len(skills) > SUMMARY_LIMIT:
        lines.append(f"... and {len(skills) - SUMMARY_LIMIT} more. Before a task, search them: list_skills with a query "
                     "naming the topic (for example query: \"android gradle build\").")
    return "\n".join(lines)


def render(skill: Skill) -> str:
    """What read_skill returns for a pack: the instructions, then what else is bundled."""
    out = [skill.body or "(this skill has no instructions)"]
    if skill.files:
        shown = skill.files[:MAX_LISTED_FILES]
        out.append("\nBundled files (read one with read_skill_file):\n" + "\n".join(f"- {f}" for f in shown))
        if len(skill.files) > len(shown):
            out.append(f"... and {len(skill.files) - len(shown)} more")
    if skill.allowed_tools:
        out.append("\n(This skill lists the tools it expects: " + ", ".join(skill.allowed_tools) +
                   ". That is information only; it does not grant anything.)")
    return "\n".join(out)


def read_bundled(skill: Skill, rel: str) -> str:
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ToolError("path must be a file inside the skill's folder")
    target = safe_path(skill.directory, rel)
    if not target.is_file():
        raise ToolError(f"{rel!r} is not a file in skill {skill.name!r}")
    raw = target.read_bytes()
    if b"\x00" in raw[:4096]:
        return f"[binary file {rel}, {len(raw)} bytes]"
    text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_FILE_READ:
        text = text[:MAX_FILE_READ] + f"\n... truncated ({len(raw)} bytes in all)"
    return text


async def _read_skill_file(inp: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
    name = str(inp.get("skill") or "").strip().lower()
    skill = get(workspace, name)
    if skill is None:
        raise ToolError(f"no skill pack named {name!r}")
    return read_bundled(skill, str(inp.get("path") or ""))


def register_all() -> None:
    S = {"type": "string"}
    toolspec.register(
        {"name": "read_skill_file",
         "description": "Read a file bundled with a skill pack (a script, reference document or template), by the path the "
                        "skill's instructions or read_skill's file list gives. Reading does not run anything.",
         "input_schema": {"type": "object", "properties": {"skill": S, "path": S}, "required": ["skill", "path"]}},
        toolspec.ToolSpec("read_skill_file", "read", read_only=True, concurrency_safe=True, max_output_chars=60_000,
                          origin="registered"), _read_skill_file)


register_all()

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

**What a skill cannot do.** A skill is text. It cannot run anything by itself: a bundled script
runs only if the agent decides to run it with `run_shell`, which asks for approval like any
command. `allowed-tools` in the front matter is shown but grants nothing. Skills that come from a
repository are labelled as such, their descriptions are trimmed to one short line, and a session
that loads one is not treated differently from any other - so read what you install (see
skill_install.py for the quarantine and scan used when fetching one from the internet).
"""

from __future__ import annotations

import re
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
SKIP = {".git", "__pycache__", "node_modules"}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    directory: Path
    source: str                       # user | project
    allowed_tools: tuple = ()
    files: tuple = ()                 # bundled files, relative paths
    problems: tuple = field(default_factory=tuple)


def user_root() -> Optional[Path]:
    try:
        from bot import envfile

        home = getattr(envfile, "ABP_HOME_ACTIVE", None)
        return (Path(home) if home else Path(envfile.PROJECT_ROOT) / "data") / "skill_packs"
    except Exception:  # noqa: BLE001
        return None


def project_roots(workspace: Optional[Path]) -> list[Path]:
    if workspace is None:
        return []
    return [Path(workspace) / rel for rel in (".claude/skills", ".agents/skills", ".abp/skills")]


def _front_matter(text: str) -> tuple[dict, str, Optional[str]]:
    from bot.agent_runtime.agent_defs import _split_front_matter

    return _split_front_matter(text)


def load_dir(directory: Path, source: str) -> Optional[Skill]:
    md = directory / "SKILL.md"
    try:
        if not md.is_file() or md.stat().st_size > MAX_SKILL_MD * 4:
            return None
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
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
    files = []
    for p in sorted(directory.rglob("*")):
        if p.is_file() and p.name != "SKILL.md" and not any(part in SKIP for part in p.relative_to(directory).parts):
            files.append(p.relative_to(directory).as_posix())
    return Skill(name, description[:1024], body.strip()[:MAX_SKILL_MD], directory, source,
                 tuple(str(t) for t in tools_field), tuple(files), tuple(problems))


def _scan(root: Path, source: str, into: dict[str, Skill]) -> None:
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return
    for d in entries:
        s = load_dir(d, source)
        if s is not None:
            into[s.name] = s


def discover(workspace: Optional[Path] = None) -> dict[str, Skill]:
    """Every skill pack visible from `workspace`; your own win over a repository's."""
    project: dict[str, Skill] = {}
    for root in project_roots(workspace):
        _scan(root, "project", project)
    user: dict[str, Skill] = {}
    ur = user_root()
    if ur is not None:
        _scan(ur, "user", user)
    return {**project, **user}


def get(workspace: Optional[Path], name: str) -> Optional[Skill]:
    return discover(workspace).get(str(name or "").strip().lower())


def summary(workspace: Optional[Path]) -> str:
    skills = sorted(discover(workspace).values(), key=lambda s: (s.source != "user", s.name))
    if not skills:
        return ""
    lines = ["Skill packs (folders of instructions; load one with read_skill, and its bundled files with read_skill_file):"]
    for s in skills[:40]:
        desc = s.description[:DESCRIPTION_SHOWN] or "(no description)"
        lines.append(f"- {s.name}: {desc}" + (" [from this project]" if s.source == "project" else ""))
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

"""Slash commands you write as Markdown files (roadmap P4).

Drop a file such as `.claude/commands/review.md` (or `.abp/commands/`, or `<ABP data>/commands/`)
in the working directory:

    ---
    description: Review the current changes
    argument-hint: [area]
    ---
    Review the uncommitted changes, focusing on $ARGUMENTS. Report real problems first.

Then `/review the login flow` in any chat sends the file's text to the agent with `$ARGUMENTS`
replaced by "the login flow" (and `$1`, `$2`, ... by the individual words). It is exactly as if
you had typed that text yourself: a command file is a saved prompt, nothing more. It cannot
grant permissions or bypass approvals.

Built-in commands always win over a file of the same name, and your own commands
(`<ABP data>/commands/`) win over a repository's. Files from a repository are text from
wherever the repository came from; they are listed by `/commands` marked as such.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_BYTES = 30_000
MAX_EXPANDED = 20_000


@dataclass(frozen=True)
class CustomCommand:
    name: str
    description: str
    template: str
    argument_hint: str
    source: str                # user | project
    origin: str


def _data_dir() -> Optional[Path]:
    try:
        from bot import envfile

        home = getattr(envfile, "ABP_HOME_ACTIVE", None)
        return Path(home) if home else Path(envfile.PROJECT_ROOT) / "data"
    except Exception:  # noqa: BLE001
        return None


def _load(folder: Path, source: str, into: dict[str, CustomCommand]) -> None:
    from bot.agent_runtime.agent_defs import _split_front_matter

    try:
        files = sorted(folder.glob("*.md"))
    except OSError:
        return
    for path in files:
        name = path.stem.lower()
        if not NAME_RE.match(name):
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body, _problem = _split_front_matter(text) if text.lstrip("﻿").startswith("---") else ({}, text, None)
        into[name] = CustomCommand(
            name, " ".join(str(meta.get("description") or "").split())[:200] or "(custom command)", body.strip(),
            str(meta.get("argument-hint") or meta.get("argument_hint") or ""), source, str(path))


def discover(workspace: Optional[Path] = None) -> dict[str, CustomCommand]:
    project: dict[str, CustomCommand] = {}
    if workspace is not None:
        for rel in (".claude/commands", ".opencode/command", ".opencode/commands", ".abp/commands"):
            _load(Path(workspace) / rel, "project", project)
    user: dict[str, CustomCommand] = {}
    data = _data_dir()
    if data is not None:
        _load(data / "commands", "user", user)
    return {**project, **user}


def expand(name: str, args_text: str, workspace: Optional[Path] = None) -> Optional[str]:
    """The prompt for `/name args`, or None if there is no such custom command."""
    cmd = discover(workspace).get(str(name or "").lower())
    if cmd is None or not cmd.template:
        return None
    words = args_text.split()
    text = cmd.template.replace("$ARGUMENTS", args_text.strip())

    def positional(m: re.Match) -> str:
        i = int(m.group(1))
        return words[i - 1] if 0 < i <= len(words) else ""

    text = re.sub(r"\$([1-9])\b", positional, text)
    if "$ARGUMENTS" not in cmd.template and args_text.strip() and not re.search(r"\$[1-9]\b", cmd.template):
        text += f"\n\n{args_text.strip()}"                 # arguments given to a command that has no place for them
    return text[:MAX_EXPANDED]


def listing(workspace: Optional[Path] = None) -> str:
    cmds = sorted(discover(workspace).values(), key=lambda c: (c.source != "user", c.name))
    if not cmds:
        return ""
    lines = ["Custom commands:"]
    for c in cmds:
        hint = f" {c.argument_hint}" if c.argument_hint else ""
        lines.append(f"  /{c.name}{hint} - {c.description}" + (" [from this project]" if c.source == "project" else ""))
    return "\n".join(lines)

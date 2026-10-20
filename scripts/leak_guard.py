#!/usr/bin/env python
"""The public-repo leak guard: nothing machine-specific in a repo anyone can clone.

ABP is public, so what is tracked in it must be true on every machine. Two kinds of thing
break that, and this scans for both:

  * machine paths — an absolute drive-letter path, or a user profile carrying a real account
    name. `<drive>:/data/...`, `<repo-root>/...`, `C:/Users/<name>/...`;
  * private terms — whatever <ABP_PRIVATE_DIR>/denylist.txt lists. That file stays private;
    ABP only ever reads it.

Each category has a mode, `fail` or `warn`, set in <ABP_PRIVATE_DIR>/config/leak_guard.yaml:

    machine_paths: fail      # or warn while a move is still in progress
    private_terms: warn      # or fail once the private integrations are out of the public repo

With no private folder both default to `fail` for machine paths and `warn` for private terms
(the default install has no denylist at all, so there is nothing private to match). Modes and
any allowlist live in the private config; nothing private is read from or written to the repo.

What it scans: every file git tracks (as the index holds it, not the working tree), or — in pre-commit mode — only the lines the staged diff
adds, so an existing match in a file nobody touched doesn't block an unrelated commit. Binary
files and vendored/generated trees (requirements.lock, the OpenAPI dump, bundled JS libraries)
are skipped: they are generated elsewhere and never hand-edited here.

    python scripts/leak_guard.py                # every tracked file
    python scripts/leak_guard.py --staged       # what the index is about to record
    python scripts/leak_guard.py --path bot/x.py --line 42     # one finding, for a test
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent

# --- category: machine paths ---------------------------------------------------------
# An absolute drive-letter path. A single letter is what makes it absolute, so this matches
# "<drive>:/data/thing" and "<repo>/Thing" but not a source-code line ending or a string escape:
# the separator is a slash or a backslash, and a backslash that opens an escape (\n, \t, \r) is
# one of those and not a path separator at all.
_SEPARATOR = r"(?:(?!\\[ntr])\\\\|(?!\\[ntr])\\|/)"
DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:" + _SEPARATOR)
# A user profile directory naming an account: C:\Users\<name>\AppData. The account itself is
# group 1 so a placeholder can be recognised below.
USER_PROFILE = re.compile(r"(?i)[A-Za-z]:" + _SEPARATOR + r"Users" + _SEPARATOR + r"([^\\/\"'`,;|)\(\s]+)")

MACHINE_PATTERNS = {"drive-path": DRIVE_PATH, "user-profile": USER_PROFILE}

# Windows' own folders and bare drive roots are not machine-specific: every Windows machine has
# them, and a test of Windows path handling has to name them. Neither is a placeholder account
# name (`<you>` in a doc, `someone` in a fixture) - those say "some account", not this one.
# Anything else after the drive letter is somebody's folder, which is what the guard is for.
_GENERIC_SEGMENTS = {"windows", "program files", "program files (x86)", "programdata", "perflogs",
                     "system volume information", "$recycle.bin"}
_GENERIC_ACCOUNTS = {
    "you", "your", "someone", "somebody", "user", "username", "name", "example", "test",
    "dev", "developer", "me", "u", "x", "abc", "aaa", "user1", "john.doe", "jdoe", "u1",
}
_PATH_TRIM = "\"'`,;|)("


def _path_at(line: str, start: int) -> str:
    """The whole path starting at `start`: from the match to the first character that cannot be
    part of one. The regex alone only proves a drive letter and a separator are there - whether
    the folder after it is Windows' own or somebody's is the question, and that needs the rest.
    One exception to "whitespace ends it": "Program Files" is a single folder name."""
    end = len(line)
    i = start
    while i < len(line):
        ch = line[i]
        if ch in _PATH_TRIM:
            end = i
            break
        if ch.isspace():
            if line[start:i].rstrip().endswith("Program") and line[i + 1:i + 2] not in ("", "\\", "/"):
                i += 1
                continue
            end = i
            break
        i += 1
    else:
        end = len(line)
    return line[start:end]


def _is_generic(path: str) -> bool:
    """Whether a drive path is Windows itself rather than somebody's folder."""
    parts = [p for p in re.split(r"[\\/]+", path.strip(_PATH_TRIM)) if p]
    if len(parts) < 2:
        return True                                   # a bare drive root, "C:\" or "C:/"
    first = parts[1]
    if first.startswith(("<", "{", "$")):
        return True                                   # a documented placeholder: "C:\<drive>\..."
    if first.lower() in _GENERIC_SEGMENTS:
        return True                                   # C:\Windows\..., C:\Program Files\...
    if first.lower() == "program" and len(parts) > 2 and parts[2].lower().startswith("files"):
        return True                                   # "Program Files" has a space in it
    if first.lower() == "users":
        return len(parts) < 3 or _is_placeholder(parts[2])
    return False

# --- what is not scanned --------------------------------------------------------------
# Generated or vendored: written by a tool from another repo, never edited here, and a
# diff-based pre-commit hook would only ever produce noise from them.
SKIP_GLOBS = (
    "requirements.lock", "*.lock", "docs/api/openapi.json", "**/vendor/**",
    "browser-extension/dist/**", "desktop-app/ui/vendor/**", "*.min.js", "*.map",
    "*.png", "*.jpg", "*.ico", "*.woff", "*.woff2", "*.pdf", "*.zip", "*.bin",
    ".git/**", "data/**", "logs/**", "node_modules/**", ".venv/**",
    # The guard's own tests: their fixtures are the very things it must catch.
    "tests/test_leak_guard.py",
)

MODES = ("fail", "warn")
DEFAULT_MODES = {"machine_paths": "fail", "private_terms": "warn"}


@dataclass(frozen=True)
class Finding:
    """One match: which file, which line, which rule, and the text that tripped it."""

    path: str
    line: int
    rule: str
    category: str
    match: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.category}/{self.rule}] {self.match!r} in {self.text.strip()[:120]}"


# --------------------------------------------------------------------------------------
# Configuration (private: modes and the allowlist are never in the public repo)
# --------------------------------------------------------------------------------------


def _yaml():
    try:
        import yaml

        return yaml
    except ImportError:  # pragma: no cover - PyYAML is a hard dependency of ABP, but the guard must still run
        return None


def settings(private_dir: Optional[Path] = None) -> dict:
    """The guard's modes and allowlist, from <ABP_PRIVATE_DIR>/config/leak_guard.yaml.

    Read from the private folder directly rather than through bot.config: the guard runs before
    ABP is importable in some contexts (a pre-commit hook on a fresh checkout, another repo's
    own CI) and must never need the package it is guarding."""
    modes = dict(DEFAULT_MODES)
    allow: list[str] = []
    path = (private_dir / "config" / "leak_guard.yaml") if private_dir else None
    if path is None or not path.is_file():
        return {"modes": modes, "allow": allow}
    yaml = _yaml()
    if yaml is None:
        return {"modes": modes, "allow": allow}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - a broken private config must not make the guard lie
        print(f"warning: couldn't read {path}; using the default modes", file=sys.stderr)
        return {"modes": modes, "allow": allow}
    if not isinstance(data, dict):
        return {"modes": modes, "allow": allow}
    for key, value in (data.get("modes") or {}).items():
        if key in modes and str(value) in MODES:
            modes[key] = str(value)
    allow = [str(p) for p in (data.get("allow") or [])]
    return {"modes": modes, "allow": allow}


def _denylist(private_dir: Optional[Path]) -> list[tuple[str, re.Pattern]]:
    """The private term list as compiled patterns. Blank lines and # comments are skipped; a
    term that isn't a valid regex is matched literally, because a typo in a private list must
    not silently disable the check."""
    if private_dir is None:
        return []
    path = private_dir / "denylist.txt"
    if not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        term = raw.strip()
        if not term or term.startswith("#"):
            continue
        try:
            out.append((term, re.compile(term, re.IGNORECASE)))
        except re.error:
            out.append((term, re.compile(re.escape(term), re.IGNORECASE)))
    return out


# --------------------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------------------


def is_skipped(rel: str) -> bool:
    posix = rel.replace("\\", "/")
    return any(fnmatch.fnmatch(posix, pat) or posix.startswith(pat.rstrip("*").rstrip("/") + "/")
               if pat.endswith("/") else fnmatch.fnmatch(posix, pat)
               for pat in SKIP_GLOBS)


def _is_placeholder(name: str) -> bool:
    """Whether a profile segment names some account rather than this one. Angle brackets, braces
    and a $ or % sigil are a placeholder's usual shape (`<you>`, `{user}`, `$USERNAME`); the bare
    words cover fixtures."""
    return (name.startswith(("<", "{", "$", "%")) or not name
            or name.strip("<>{}%$ ").lower() in _GENERIC_ACCOUNTS)


def scan_text(rel: str, text: str, rules: list[tuple[str, str, re.Pattern]], lineno: int = 1) -> Iterator[Finding]:
    """Every match in one file's text, reported from line `lineno` (a pre-commit hook scans one
    added line at a time and has to report where it really is). `rules` is
    (category, name, pattern)."""
    base = lineno
    for offset, line in enumerate(text.splitlines()):
        lineno = base + offset
        if len(line) > 4000:
            # A minified bundle or a base64 blob: a match inside one is not a path anyone wrote.
            line = line[:4000]
        for category, rule, pattern in rules:
            for m in pattern.finditer(line):
                if category == "machine_paths":
                    if rule == "user-profile":
                        if _is_placeholder(m.group(1)):
                            continue
                    elif _is_generic(_path_at(line, m.start())):
                        continue
                yield Finding(rel, lineno, rule, category, _path_at(line, m.start()), line)
                break        # one finding per rule per line: five spellings of one path is not five leaks


def _tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True,
                             check=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [p for p in out.split("\0") if p]


def _staged_lines(root: Path) -> Iterator[tuple[str, int, str]]:
    """(path, line number in the file, line text) for every line the index is about to add.
    Reading the blob out of the object store keeps this exactly what git will record, and
    keeps line numbers real — a pre-commit hook that reported offsets into a diff hunk would
    send whoever fixes the leak to the wrong line."""
    try:
        diff = subprocess.run(["git", "diff", "--cached", "--unified=0", "--no-color", "--diff-filter=ACMR"],
                              cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
                              timeout=120).stdout or ""      # utf-8 always: the locale code page cannot decode every byte
    except (OSError, subprocess.SubprocessError):
        return
    lines = diff.splitlines()
    path: Optional[str] = None
    lineno = 0
    for line in lines:
        if line.startswith("diff --git "):
            path, lineno = None, 0
        elif line.startswith("+++ b/"):
            path = line[6:].strip()
        elif line.startswith("@@"):
            m = re.match(r"^@@ -\S+ \+(\d+)(?:,\d+)? @@", line)
            lineno = int(m.group(1)) if m else 0
        elif path is None or lineno == 0:
            continue
        elif line.startswith("+") and not line.startswith("+++"):
            yield path, lineno, line[1:]
            lineno += 1
        elif line.startswith(" "):
            lineno += 1


def _staged_paths(root: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
                             cwd=root, capture_output=True, text=True, check=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [p for p in out.split("\0") if p]


def _read(repo_root: Path, rel: str) -> Optional[str]:
    """The committed-to-the-index content of a tracked file (what a clone would get), not the working tree: local
    uncommitted edits (a machine's own config) are not what the guard guards."""
    try:
        data = subprocess.run(["git", "show", f":{rel}"], cwd=repo_root, capture_output=True, check=True,
                              timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _build_rules(private_dir: Optional[Path]) -> list[tuple[str, str, re.Pattern]]:
    rules = [("machine_paths", name, pattern) for name, pattern in MACHINE_PATTERNS.items()]
    rules += [("private_terms", term, pattern) for term, pattern in _denylist(private_dir)]
    return rules


def scan(root: Path = ROOT, *, staged: bool = False, private_dir: Optional[Path] = None,
         paths: Optional[Iterable[str]] = None, rules: Optional[list] = None,
         allow: Optional[Iterable[str]] = None) -> list[Finding]:
    """Every finding. `paths` narrows the scan to specific files (used by the tests);
    `staged` scans only what the index would add; `allow` is the private allowlist of globs."""
    rules = _build_rules(private_dir) if rules is None else rules
    allowed = [str(p).replace("\\", "/") for p in (allow or [])]
    findings: list[Finding] = []
    if staged:
        for rel, lineno, text in _staged_lines(root):
            if is_skipped(rel) or _allowed(rel, allowed):
                continue
            findings.extend(scan_text(rel, text, rules, lineno))
        return findings
    targets = list(paths) if paths is not None else _tracked_files(root)
    for rel in targets:
        if is_skipped(rel) or _allowed(rel, allowed):
            continue
        text = _read(root, rel)
        if text is None:
            continue
        findings.extend(scan_text(rel, text, rules))
    return findings


def _allowed(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel.replace("\\", "/"), pat) for pat in patterns)


def report(findings: list[Finding], modes: dict) -> tuple[list[Finding], list[Finding]]:
    """Split findings into (must_fail, warnings) by each category's configured mode."""
    failing = [f for f in findings if modes.get(f.category, "warn") == "fail"]
    warning = [f for f in findings if modes.get(f.category, "warn") != "fail"]
    return failing, warning


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staged", action="store_true", help="scan only the lines the index is about to add")
    ap.add_argument("--root", default=str(ROOT), help="repository to scan (default: this checkout)")
    ap.add_argument("--private-dir", default=os.environ.get("ABP_PRIVATE_DIR", ""),
                    help="the private folder holding leak_guard.yaml and denylist.txt")
    ap.add_argument("--quiet", action="store_true", help="only the verdict, no per-finding lines")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    private_dir = Path(args.private_dir).expanduser() if args.private_dir else None
    cfg = settings(private_dir)
    findings = scan(root, staged=args.staged, private_dir=private_dir, allow=cfg["allow"])
    failing, warning = report(findings, cfg["modes"])

    if not args.quiet:
        for f in failing:
            print(f"  [ERR]  {f}", file=sys.stderr)
        for f in warning:
            print(f"  [!]    {f}", file=sys.stderr)
    if not findings:
        print("  [ok]   no machine paths and no private terms in the tracked files")
        return 0
    if failing:
        print(f"leak guard: {len(failing)} finding(s) that must not be committed "
              f"({len(warning)} warning(s)) - see scripts/leak_guard.py", file=sys.stderr)
        return 1
    print(f"leak guard: {len(warning)} warning(s), nothing failing", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
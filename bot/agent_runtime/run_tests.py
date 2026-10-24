"""run_tests: run pytest on named test files, for agents that have no general shell.

A local model working without `run_shell` still has to check its edit. This tool runs exactly one thing,
`python -m pytest -q -x <paths>`, and nothing else:

* every argument must be a test path or node id inside the working directory (`tests/test_x.py`,
  `tests/test_x.py::test_name`, `tests/test_x.py::TestClass::test_name[param]`); anything with a shell
  metacharacter, an option-looking argument, a path outside it or a `..` is refused. Usual slips are normalised
  ("./", backslashes, an absolute path inside the workspace, `file.py:42`, one string instead of a list, `tests`/`target`
  and similar key names), and `k` selects tests by name (letters, digits, spaces and . _ - [ ] = + , ( ) only). Every refusal
  says what is accepted, with an example;
* it starts the process through the same sandbox as `run_shell` (sandbox.start), so a configured
  Windows job / Docker / ssh / WSL sandbox and its network policy apply - it never runs outside one that is set;
* a timeout (`native_agent.run_tests.timeout_s`, default 300 s) stops the whole process tree;
* the answer is the pass/fail counts plus the failing tests' short tracebacks; tokenjuice compacts it further
  (it classes run_tests output as a log).

Approval is the normal one for a tool that runs code (tests are code): ask, unless a permission rule or mode allows it.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from bot.agent_runtime import shell, toolspec
from bot.agent_runtime.errors import ToolError, safe_path

# The one backend that runs the command directly on this host with no container, so this process's own interpreter
# path is valid and needs no sandbox grant. windows_job (AppContainer) does NOT auto-grant the venv directory, and
# docker/ssh/wsl run their own guest python, so for those we keep the bare name (or an owner-configured interpreter)
# and let the container/guest resolve it. See docs: the confined case is fixed by native_agent.run_tests.python +
# sandbox.network_none.extra_paths, or by a future auto-grant of the interpreter tree.
_DIRECT_BACKENDS = ("local", "windows_job")  # run directly on this host, or the offline AppContainer now grants the interpreter tree
_interp_cache: dict[str, bool] = {}

DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 3600
MAX_TARGETS = 20
MAX_REPORT_CHARS = 8000

# A path (no drive letter, so no ':'), optionally followed by '::' parts. Nothing a shell treats specially: no spaces,
# quotes, & | ; < > ^ % $ ` ( ) { } ! * ? ~ #. '[' ']' ',' '=' '+' are allowed for parametrised ids.
_PATH = re.compile(r"^[\w./\\-]+$")
_ID_PART = re.compile(r"^[\w.\-\[\],=+/]+$")
_COUNTS = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed|warnings?|deselected)")
_EXIT = re.compile(r"\n?\[exit code (-?\d+)\]\s*$")


def _config() -> dict:
    try:
        from bot.config import config

        return ((config.current.get("native_agent") or {}).get("run_tests")) or {}
    except Exception:  # noqa: BLE001
        return {}


def timeout_s() -> int:
    try:
        seconds = int(_config().get("timeout_s", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        seconds = DEFAULT_TIMEOUT_S
    return max(1, min(seconds, MAX_TIMEOUT_S))


# Names a model has been seen to use for the list of targets; `paths` is the documented one.
PATH_KEYS = ("paths", "path", "test_paths", "test_path", "tests", "test", "test_files", "test_file", "files", "file",
             "targets", "target", "node_ids", "nodeids")
K_KEYS = ("k", "keyword", "expression")
_K = re.compile(r"^[\w ._\-\[\]=+,()]+$")
_RUNNER_PREFIX = re.compile(r"^\s*(?:python3?(?:\.exe)?\s+-m\s+)?pytest\s+", re.I)
SHAPE = ('Call run_tests with {"paths": ["tests/test_x.py", "tests/test_y.py::TestClass::test_z[1-a]"]} - repository-relative '
         'test files, folders or node ids (one string per target; "./" and backslashes are fine); to pick tests by name add '
         '{"k": "name_part and not other"}. No options, no shell syntax.')


def _relative(workspace: Path, path: str) -> str:
    """`path` as a workspace-relative forward-slash path: backslashes, a leading "./" and an absolute path that points
    inside the workspace are normalised. An absolute path elsewhere is left as it is (and refused later)."""
    path = path.replace("\\", "/")
    if re.match(r"^(?:[A-Za-z]:)?/", path):
        try:
            return Path(path).resolve().relative_to(workspace).as_posix()
        except (ValueError, OSError):
            return path
    while path.startswith("./"):
        path = path[2:]
    return path


def pick_inputs(inp: dict):
    """(raw targets, k expression) from the tool input, accepting the usual alternative key names."""
    raw = next((inp[key] for key in PATH_KEYS if inp.get(key) not in (None, "", [])), None)
    k = next((inp[key] for key in K_KEYS if inp.get(key) not in (None, "")), None)
    return raw, k


def clean_k(k) -> str:
    if k is None:
        return ""
    if not isinstance(k, str) or not _K.match(k.strip()) or k.strip().startswith("-") or len(k) > 200:
        raise ToolError(f"k {k!r} is not accepted: letters, digits, spaces and . _ - [ ] = + , ( ) only, at most 200 characters, "
                        'e.g. {"k": "parse and not slow"}')
    return k.strip()


def clean_targets(workspace: Path, raw, must_exist: bool = True) -> list[str]:
    """The validated test targets, normalised. Raises ToolError naming the first bad one and the accepted shape."""
    workspace = Path(workspace).resolve()
    if isinstance(raw, str):
        text = _RUNNER_PREFIX.sub("", raw.strip())
        raw = [t.rstrip(",") for t in text.split() if t.strip(",")] if text else []
    if not isinstance(raw, list) or not raw:
        raise ToolError("paths must be a non-empty list of test files or node ids. " + SHAPE)
    if len(raw) > MAX_TARGETS:
        raise ToolError(f"at most {MAX_TARGETS} paths per call. " + SHAPE)
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ToolError("each path must be a non-empty string. " + SHAPE)
        target = item.strip()
        if len(target) > 1 and target[0] == target[-1] and target[0] in "\"'":      # one pair of wrapping quotes
            target = target[1:-1].strip()
        path, sep, ident = target.partition("::")
        m = re.match(r"^(.*\.py):\d+$", path)              # file.py:42 -> the file
        if m:
            path = m.group(1)
        path = _relative(workspace, path)
        target = path + sep + ident
        if path.startswith("-"):
            raise ToolError(f"{item!r} looks like an option; run_tests takes no options (use the \"k\" field to select tests by name). " + SHAPE)
        if not path or not _PATH.match(path) or ".." in re.split(r"[\/]", path):
            raise ToolError(f"{item!r} is not a test path inside the working directory (letters, digits, . _ - / only; "
                            "no '..', no paths outside it, no shell syntax). " + SHAPE)
        if Path(path).is_absolute() or path.startswith(("/", "\\")):
            raise ToolError(f"{item!r} must be inside the working directory (give it relative to it). " + SHAPE)
        if ident and not all(_ID_PART.match(p) for p in ident.split("::")):
            raise ToolError(f"{item!r}: the node id after '::' may only hold letters, digits and . _ - [ ] , = + /. " + SHAPE)
        resolved = safe_path(workspace, path)
        if not must_exist:                  # shape check only (the conductor validates a plan's proof before any workspace exists)
            out.append(target)
            continue
        if not resolved.exists():
            near = sorted(x.relative_to(workspace).as_posix() for x in workspace.rglob(Path(path).name)
                          if x.is_file() and ".git" not in x.parts and "node_modules" not in x.parts)[:5] if Path(path).name else []
            hint = f" Did you mean: {', '.join(near)}?" if near else " List the tests folder with list_dir or glob first."
            raise ToolError(f"{path!r} does not exist.{hint} " + SHAPE)
        if resolved.is_file() and resolved.suffix != ".py":
            raise ToolError(f"{path!r} is not a Python test file. " + SHAPE)
        out.append(target)
    return out


def _has_pytest(exe: str) -> bool:
    """True if `exe` can import pytest. Cached per interpreter — the probe runs outside the sandbox, only to
    choose which interpreter the sandboxed command names."""
    if exe in _interp_cache:
        return _interp_cache[exe]
    try:
        ok = subprocess.run([exe, "-c", "import pytest"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    _interp_cache[exe] = ok
    return ok


def _pytest_python() -> str:
    """The interpreter the pytest command should name.

    An explicit `native_agent.run_tests.python` wins on any backend (the owner points it at a pytest-capable
    interpreter the sandbox can reach). Otherwise, only for the `local` backend — which runs the command directly
    on this host with no container — do we substitute a verified interpreter (this process's own, then PATH's
    python/python3), so a stale pyenv that happens to come first on PATH without pytest cannot shadow it (a
    stray pyenv that happens to precede the project venv on PATH). For a container/remote backend (windows_job AppContainer, docker,
    ssh, wsl) the bare name is correct: the guest resolves it, and naming an un-granted host path would be denied.
    Always falls back to the bare name, so it is never worse than before."""
    configured = str(_config().get("python") or "").strip()
    if configured and _has_pytest(configured):
        return configured
    try:
        from bot.agent_runtime import sandbox

        direct = sandbox.backend() in _DIRECT_BACKENDS
    except Exception:  # noqa: BLE001  (config unreadable: assume the direct/local case, the common one)
        direct = True
    if direct:
        for exe in (sys.executable, shutil.which("python"), shutil.which("python3")):
            if exe and _has_pytest(exe):
                return exe
    return "python"


def build_command(targets: list[str], k: str = "") -> str:
    """`<python> -m pytest -q -x -p no:cacheprovider --tb=short -rfE [-k EXPR] <targets>`, quoted for the platform's
    shell. `<python>` is an interpreter verified to have pytest on a host-local sandbox (see _pytest_python), or the
    bare name `python` for a container/remote backend."""
    py = _pytest_python()
    args = ["-q", "-x", "-p", "no:cacheprovider", "--tb=short", "-rfE", *(["-k", k] if k else []), *targets]
    if os.name == "nt":
        # Always quoted: inside the offline AppContainer, cmd.exe given an UNQUOTED absolute path probes its parent
        # folders (which the container may not read) and reports "Access is denied" before python starts.
        return f'"{py}" -m pytest ' + subprocess.list2cmdline(args)
    return shlex.quote(py) + " -m pytest " + shlex.join(args)


def summarise(output: str) -> str:
    """Counts, then the failures' short tracebacks and the failed ids, from a pytest -q run."""
    code = None
    m = _EXIT.search(output)
    if m:
        code = int(m.group(1))
        output = output[:m.start()]
    lines = output.splitlines()
    counts = ""
    for line in reversed(lines):
        found = _COUNTS.findall(line)
        if found and (" in " in line or line.startswith("=")):
            counts = ", ".join(f"{n} {word}" for n, word in found)
            break
    failures: list[str] = []
    failed_ids = [x for x in lines if x.startswith(("FAILED ", "ERROR "))]
    start = next((i for i, x in enumerate(lines) if re.match(r"^=+ (FAILURES|ERRORS) =+$", x)), None)
    if start is not None:
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^=+ .* =+$", lines[i])), len(lines))
        failures = lines[start + 1:end]
    if code == 0:
        verdict = "PASSED"
    elif code == 5:
        verdict = "NO TESTS COLLECTED"
    elif code in (1, 2, 3, 4):
        verdict = "FAILED" if code == 1 else "ERROR"
    else:
        verdict = "ERROR"
    head = f"{verdict}: {counts or 'no summary line'} (exit code {code})"
    body = "\n".join(failures).strip()
    if verdict == "PASSED" or not body:           # no traceback section (a collection or usage error): show the tail as it is
        tail = "" if verdict == "PASSED" else "\n" + "\n".join(lines[-40:])
        return head + tail
    if len(body) > MAX_REPORT_CHARS:
        body = body[:MAX_REPORT_CHARS] + "\n... (traceback trimmed)"
    return head + ("\n\n" + body if body else "") + ("\n\n" + "\n".join(failed_ids) if failed_ids else "")


async def _run_tests(inp: dict, *, workspace: Path, instance_id=None, device_tier=None) -> str:
    workspace = Path(workspace).resolve()
    raw, k = pick_inputs(inp)
    targets = clean_targets(workspace, raw)
    command = build_command(targets, clean_k(k))
    limit = timeout_s()
    output = await shell._run_fresh(command, workspace, workspace, limit)      # the sandbox, timeout and tree-kill live here
    return summarise(output)


def register_all() -> None:
    toolspec.register(
        {"name": "run_tests",
         "description": "Run pytest on test files or node ids inside the working directory and get the pass/fail counts and "
                        "the failing tests' short tracebacks. Runs `python -m pytest -q -x` (stops at the first failure) "
                        "in the configured sandbox. Pass paths such as [\"tests/test_x.py\", \"tests/test_y.py::test_z\"]; "
                        "optional \"k\" picks tests by name (e.g. \"parse and not slow\"). No other options and no other commands.",
         "input_schema": {"type": "object",
                         "properties": {"paths": {"type": "array", "items": {"type": "string"},
                                                  "description": "Test files, folders or node ids, relative to the working directory."},
                                       "k": {"type": "string", "description": "Optional: only tests whose name matches this pytest -k expression."}},
                         "required": ["paths"]}},
        toolspec.ToolSpec("run_tests", "execute", read_only=False, max_output_chars=12_000, origin="registered"),
        _run_tests)


register_all()

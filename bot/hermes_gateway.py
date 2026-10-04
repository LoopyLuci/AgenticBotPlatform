"""Hermes Agent's OWN messaging gateway, seen from the outside: whose Telegram
token does it own, is it running, and how do we start/stop/tail it — while
Hermes keeps serving Telegram on its own and AgenticBotPlatform drives it in
parallel.

This is the other half of docs/connecting-claude-and-hermes.md's "shared
platform tokens" warning. The warning says *pick one owner per token*, and
historically that meant "AgenticBotPlatform owns it and Hermes's gateway must
be edited out of the picture". Plenty of users want the opposite: they talk to
Hermes directly on Telegram every day through Hermes's own gateway, and they
also want AgenticBotPlatform to exist alongside it. Both can be true, but only
if ABP never polls a token Hermes's gateway is already polling — a Telegram bot
token's `getUpdates` can have exactly ONE long-poller, and a second one gets
HTTP 409 and the two knock each other off every few seconds.

So two halves:

**Token ownership (read-only, cheap, cached).** `telegram_owner()` answers "is
this token one a running Hermes gateway is already serving?" by reading the
same files Hermes's own `hermes gateway status` reads — `<HERMES_HOME>/.env`
for an uncommented `TELEGRAM_BOT_TOKEN`, `<HERMES_HOME>/config.yaml` for
`platforms.telegram.enabled`, and `<HERMES_HOME>/gateway.pid` +
`gateway_state.json` for the gateway process itself. Tokens are compared by
sha256 digest and never logged, returned in an API payload, or written
anywhere: the only thing that leaves this module is the digest and the Hermes
home directory.

**Gateway control (the same commands the user already types).** `status()`,
`list_profiles()`, `start()`, `stop()`, `restart()`, `logs()` and `ask()` shell
out to the real `hermes` CLI, windowless (`CREATE_NO_WINDOW`), so the
dashboard, the CLI and the Support Bot all see exactly what `hermes gateway
status` in a terminal would show. `start()` deliberately does NOT just spawn a
child of ABP: Hermes's own `hermes gateway status` warns that a gateway started
from a shell inside a Windows Job Object gets killed when that shell exits
(#91675), and its fix is the login item it already installs
(`<HERMES_HOME>/gateway-service/Hermes_Gateway.vbs`, run hidden and
non-blocking, i.e. outside ABP's process tree). `start()` reuses that launcher
when it exists, and falls back to a detached, hidden spawn with the same env it
sets.

Everything here is read-only or explicitly requested by an operator
(dashboard/CLI/API). Nothing in this module ever starts, stops or restarts a
gateway on its own — see bot/platform_supervisor.py, which only ever *reads*
`telegram_owner()` and then declines to poll.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("bot.hermes_gateway")

# Hermes's own state files, confirmed against the installed Hermes Agent
# source (gateway/status.py): the pid file is <HERMES_HOME>/gateway.pid (a JSON
# object, or a legacy bare integer), and the runtime record is
# <HERMES_HOME>/gateway_state.json (gateway_status._RUNTIME_STATUS_FILE).
GATEWAY_PID_FILE = "gateway.pid"
GATEWAY_STATE_FILE = "gateway_state.json"
# gateway/status.py's _GATEWAY_KIND — the marker a pid record must carry before
# its recorded argv is trusted as gateway identity.
_GATEWAY_KIND = "hermes-gateway"

# Only the token-to-home mapping is memoised, never the running flag: which home
# owns a token is a property of a file that does not change while a gateway
# runs, whereas "is that gateway up right now" is exactly the answer that has to
# stay fresh (an instance with takeover_when_gateway_down must start polling the
# moment the gateway exits, and a dashboard must not show a gateway that has
# since been stopped). Five seconds is short enough for that and long enough to
# keep a dashboard poll over a dozen bot instances to a couple of file reads.
_OWNERSHIP_CACHE_TTL_S = 5.0
_ownership_cache: dict[tuple, tuple[float, Any]] = {}

# `hermes gateway status` can take a while on a cold profile (it stats a
# process table on Windows); generous, but never unbounded.
_CLI_TIMEOUT_S = 60.0
_START_TIMEOUT_S = 180.0

CONFLICT_ERROR = "another program is polling this bot (409 Conflict)"
SERVED_BY_PREFIX = "served by the Hermes gateway"
# What an instance says while it is parked on a token whose owning gateway is
# not running. Distinct from SERVED_BY_PREFIX on purpose: nothing is serving
# that bot right now, and saying otherwise would be a lie. Only an instance
# that has actually parked (i.e. that saw the gateway running first) shows
# this - see platform_supervisor._run_telegram.
GATEWAY_DOWN_TEMPLATE = (
    "not polling: the Hermes gateway that owns this token is not running ({home}) - "
    "set takeover_when_gateway_down to take the token over"
)


class GatewayError(RuntimeError):
    """A `hermes gateway ...` call failed (binary missing, non-zero exit)."""


@dataclass(frozen=True)
class TelegramOwner:
    """A Hermes home whose gateway holds this Telegram token.

    `token_sha256` is the ONLY representation of the token that ever leaves
    this module — see the module docstring."""

    home: str
    token_sha256: str
    running: bool
    enabled: bool = True

    @property
    def status_text(self) -> str:
        """Exactly what the dashboard/CLI shows for an instance ABP is not polling."""
        return f"{SERVED_BY_PREFIX} ({self.home})"

    def to_dict(self) -> dict[str, Any]:
        return {"home": self.home, "token_sha256": self.token_sha256, "running": self.running, "enabled": self.enabled}


# --------------------------------------------------------------- homes ----

def default_home() -> Path:
    """The machine-wide Hermes home this process would hand to a
    hermes_gateway-backed instance with no per-instance override. Delegates to
    bot.hermes_config so there is exactly one HERMES_HOME resolution in this
    project (that module's docstring records what went wrong when there were
    two) and so it is re-read per call rather than frozen at import."""
    from bot.hermes_config import _default_hermes_home

    return _default_hermes_home()


def _instance_homes() -> list[Path]:
    """Every `hermes_home` any configured bot instance names."""
    from bot import bot_instances

    out: list[Path] = []
    try:
        rows = bot_instances.list_instances()
    except Exception:
        logger.debug("could not list bot instances for hermes homes", exc_info=True)
        return out
    for row in rows:
        home = (row.get("hermes_home") or "").strip()
        if home:
            out.append(Path(home).expanduser())
    return out


def homes(extra: Optional[list[Any]] = None) -> list[Path]:
    """Every Hermes home worth checking for a Telegram token: the default one,
    plus every per-instance `hermes_home`, de-duplicated by resolved path
    (case-insensitively on Windows, where the same home is reachable spelled
    two ways). Order is stable: default first."""
    seen: set[str] = set()
    out: list[Path] = []
    for candidate in [default_home(), *_instance_homes(), *(Path(str(e)) for e in (extra or []))]:
        try:
            key = os.path.normcase(str(candidate.expanduser().resolve(strict=False)))
        except OSError:
            key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _home_key(home: Any) -> str:
    try:
        return os.path.normcase(str(Path(home).expanduser().resolve(strict=False)))
    except (OSError, TypeError):
        return os.path.normcase(str(home))


# ------------------------------------------------------------- token read --

def token_fingerprint(token: str) -> str:
    """The one and only representation of a bot token that leaves this module."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_dotenv(path: Path) -> dict[str, str]:
    """The uncommented `KEY=value` pairs of a Hermes `.env`.

    Hermes loads that file with python-dotenv (hermes_cli/env_loader.py), whose
    rules this mirrors rather than reimplements loosely: `#` starts a comment,
    an `export ` prefix is dropped, surrounding quotes are stripped and inline
    `\n` escapes are expanded. Nothing here is logged or returned to a caller —
    the only caller (telegram_token) immediately reduces it to a digest."""
    out: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote, value = value[0], value[1:-1]
            if quote == '"':
                value = value.replace("\\n", "\n").replace("\\r", "\r")
        out[key] = value
    return out


def telegram_token(home: Any) -> str:
    """The `TELEGRAM_BOT_TOKEN` Hermes's gateway would serve for this home."""
    return read_dotenv(Path(home) / ".env").get("TELEGRAM_BOT_TOKEN", "").strip()


def telegram_enabled(home: Any) -> Optional[bool]:
    """`platforms.telegram.enabled` in this home's config.yaml, or None when
    the key is absent.

    Absent is not the same as off: Hermes's own rule
    (gateway/config_env.py's `_enable_from_env`) is "credentials in .env enable
    the platform unless config.yaml explicitly disables it", so only an
    explicit `false` means Hermes's gateway will not serve Telegram here.
    Returns None for an unreadable/absent file rather than guessing."""
    path = Path(home) / "config.yaml"
    if not path.is_file():
        return None
    try:
        from ruamel.yaml import YAML

        yaml = YAML(typ="safe")  # read-only here; bot/hermes_config.py owns the round-trip writer
        with path.open(encoding="utf-8") as f:
            data = yaml.load(f) or {}
    except Exception as exc:
        logger.warning("could not read %s: %s", path, exc)
        return None
    telegram = (data.get("platforms") or {}).get("telegram") or {}
    if not isinstance(telegram, dict) or "enabled" not in telegram:
        return None
    value = telegram.get("enabled")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


# ------------------------------------------------------- gateway process --

def _read_json(path: Path) -> Optional[dict[str, Any]]:
    """Hermes's own `_read_json_file` shape: a JSON object, or a legacy
    bare-integer pid file, or nothing at all (absent/empty/corrupt — all of
    which Hermes treats as "no record", never as an error)."""
    try:
        raw = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        try:
            payload = int(raw)
        except ValueError:
            return None
    if isinstance(payload, int):
        return {"pid": payload}
    return payload if isinstance(payload, dict) else None


def _pid_alive(pid: int) -> bool:
    """Whether that pid is a live process. psutil first (it distinguishes a
    zombie from a running process on POSIX, which a bare `os.kill(pid, 0)`
    cannot); a plain existence probe when psutil is unavailable."""
    try:
        import psutil

        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except ImportError:
        pass
    except Exception:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _process_cmdline(pid: int) -> Optional[str]:
    try:
        import psutil

        return " ".join(psutil.Process(pid).cmdline())
    except Exception:
        return None


def _looks_like_gateway(cmdline: str) -> bool:
    """Whether a command line is a real `hermes gateway run` process.

    Deliberately as strict as Hermes's own matcher
    (gateway/status.py's `looks_like_gateway_command_line`): it requires a
    Hermes entrypoint (`hermes`, `hermes.exe`, `hermes_cli.main`,
    `hermes-gateway`) AND the `gateway` subcommand (bare `gateway` counts as
    `run`). A loose `"gateway" in cmdline` would happily accept the `hermes
    gateway status` we are about to run ourselves, or any unrelated program
    with the word in it."""
    try:
        tokens = [t.strip("\"'").replace("\\", "/").lower() for t in shlex.split(cmdline, posix=False)]
    except ValueError:
        tokens = cmdline.lower().replace("\\", "/").split()
    if not tokens:
        return False
    basenames = [t.rsplit("/", 1)[-1] for t in tokens]
    if basenames[0] == "osascript":
        return False
    if any(t == "gateway/run.py" or t.endswith("/gateway/run.py") for t in tokens):
        return True
    if any(b in ("hermes-gateway", "hermes-gateway.exe") for b in basenames):
        return True
    joined = " ".join(tokens)
    if "hermes_cli.main" not in joined and "hermes_cli/main.py" not in joined and not any(
        b in ("hermes", "hermes.exe") for b in basenames
    ):
        return False
    filtered: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
        elif token in ("--profile", "-p"):
            skip = True
        elif not token.startswith(("--profile=", "-p=")):
            filtered.append(token)
    for i, token in enumerate(filtered):
        if token == "gateway":
            sub = filtered[i + 1] if i + 1 < len(filtered) else "run"
            return sub in ("run", "restart")
    return False


def _record_is_gateway(record: dict[str, Any]) -> bool:
    """Pid-record identity when the live command line is unreadable (Windows
    hides some processes' argv): same rule as Hermes's `_record_looks_like_gateway`
    — the record must be tagged `kind: hermes-gateway` and its own argv must
    look like a gateway run."""
    argv = record.get("argv")
    if record.get("kind") != _GATEWAY_KIND or not isinstance(argv, list) or not argv:
        return False
    return _looks_like_gateway(" ".join(str(part) for part in argv))


def gateway_pids(home: Any) -> list[int]:
    """Live Hermes gateway pids recorded for this home, from Hermes's own state
    files and nowhere else — no process-table scan (that is Hermes's job, and
    it is the expensive path this deliberately avoids on every dashboard render).

    `<HERMES_HOME>/gateway.pid` is authoritative; `gateway_state.json` is a
    fallback for a home whose pid file was lost mid-run. A recorded pid only
    counts when it is genuinely alive AND still identifies as a Hermes gateway,
    so a pid recycled onto an unrelated process is not mistaken for one."""
    home_path = Path(home)
    out: list[int] = []
    for name in (GATEWAY_PID_FILE, GATEWAY_STATE_FILE):
        record = _read_json(home_path / name)
        if not record:
            continue
        try:
            pid = int(record.get("pid"))
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid in out or not _pid_alive(pid):
            continue
        cmdline = _process_cmdline(pid)
        if cmdline:
            if not _looks_like_gateway(cmdline):
                continue
        elif not _record_is_gateway(record):
            continue
        out.append(pid)
    return out


def gateway_running(home: Any) -> bool:
    return bool(gateway_pids(home))


def gateway_state(home: Any) -> dict[str, Any]:
    """The gateway's own `gateway_state.json` (never includes a token: it holds
    pid/state/platform names only), plus the pids we could confirm alive."""
    home_path = Path(home)
    record = _read_json(home_path / GATEWAY_STATE_FILE) or {}
    return {
        "home": str(home_path),
        "pids": gateway_pids(home),
        "gateway_state": record.get("gateway_state"),
        "updated_at": record.get("updated_at"),
        "active_agents": record.get("active_agents"),
        "platforms": sorted((record.get("platforms") or {}).keys()) if isinstance(record.get("platforms"), dict) else [],
        "pid_record": _read_json(home_path / GATEWAY_PID_FILE),
    }


# ------------------------------------------------------------ ownership ----

def _cached(key: tuple, compute):
    now = time.monotonic()
    hit = _ownership_cache.get(key)
    if hit is not None and now - hit[0] < _OWNERSHIP_CACHE_TTL_S:
        return hit[1]
    value = compute()
    _ownership_cache[key] = (now, value)
    return value


def _claiming_home(token: str, search: list[Path]) -> Optional[Path]:
    """Which of `search` would serve `token` at all - the half of the ownership
    answer that cannot change under us while a gateway runs."""

    def _scan() -> Optional[str]:
        for home in search:
            home_token = telegram_token(home)
            if not home_token or token_fingerprint(home_token) != token_fingerprint(token):
                continue
            if telegram_enabled(home) is False:
                continue   # platforms.telegram.enabled: false — Hermes will not serve this token here
            return str(home)
        return None

    found = _cached(("home", token_fingerprint(token), tuple(_home_key(h) for h in search)), _scan)
    return Path(found) if found else None


def telegram_owner(token: str, candidate_homes: Optional[list[Any]] = None) -> Optional[TelegramOwner]:
    """The Hermes home whose gateway serves `token`, or None when no configured
    Hermes home claims it. An empty/absent token matches nothing (an instance
    mid-edit has no token yet; that is not evidence of anything)."""
    token = (token or "").strip()
    if not token:
        return None
    home = _claiming_home(token, homes(candidate_homes))
    if home is None:
        return None
    return TelegramOwner(
        home=str(home),
        token_sha256=token_fingerprint(token),
        running=gateway_running(home),
        enabled=telegram_enabled(home) is not False,
    )


def is_self_managed(row: dict[str, Any]) -> bool:
    """Whether this row IS its own Hermes gateway: backend `hermes_gateway`
    with the very home that owns the token.

    Such an instance is not a conflict with itself — AgenticBotPlatform manages
    that gateway (bot/backends/hermes_gateway_backend.py spawns and owns the
    `hermes serve` process, and the same home's Telegram token is what the user
    talks to on Telegram) — so it must keep polling exactly as it always has.
    Note this is about the *messaging gateway*, a different process from the
    `hermes serve` backend; the important part is that the home is the same one
    whose token is in play."""
    if (row.get("backend") or "") != "hermes_gateway":
        return False
    own = (row.get("hermes_home") or "").strip()
    if not own:
        own = str(default_home())
    owner = telegram_owner((row.get("credentials") or {}).get("bot_token", ""), [own])
    return owner is not None and _home_key(owner.home) == _home_key(own)


def instance_owner(row: dict[str, Any], candidate_homes: Optional[list[Any]] = None) -> Optional[TelegramOwner]:
    """The running Hermes gateway that owns this instance's Telegram token, or
    None when AgenticBotPlatform is free to poll it.

    Only platform=telegram has an answer (a Discord/Slack token is never a
    `getUpdates`-exclusive poller, and this module only knows about Hermes's
    Telegram config), and a self-managed hermes_gateway instance is never a
    conflict with its own home."""
    if (row.get("platform") or "") != "telegram" or is_self_managed(row):
        return None
    return telegram_owner((row.get("credentials") or {}).get("bot_token", ""), candidate_homes)


def instance_status(row: dict[str, Any], candidate_homes: Optional[list[Any]] = None) -> str:
    """What the API/dashboard/CLI shows for an instance ABP is deliberately not
    polling. Empty string when ABP owns the token, or when no running gateway
    claims it (a dead pid is not ownership — see the test). Deliberately does
    NOT report the parked-but-gateway-down case: on its own a stale token in
    some home's .env is no reason to claim ABP will never poll, and only a
    runner that has actually parked knows that (platform_supervisor)."""
    owner = instance_owner(row, candidate_homes)
    return owner.status_text if owner is not None and owner.running else ""


def served_instances() -> dict[int, dict[str, Any]]:
    """`{instance_id: owner_dict}` for every configured Telegram instance a
    running Hermes gateway is already serving. Used by the dashboard's bots
    list so an instance that is deliberately idle is still visibly accounted
    for instead of looking broken."""
    from bot import bot_instances

    out: dict[int, dict[str, Any]] = {}
    for row in bot_instances.list_instances(platform="telegram"):
        owner = instance_owner(row)
        if owner is not None and owner.running:
            out[row["id"]] = {**owner.to_dict(), "status": owner.status_text, "name": row.get("name")}
    return out


# ------------------------------------------------------- the hermes CLI ----

def hermes_binary() -> str:
    """The `hermes` executable. `shutil.which` so a test can point it at a real
    fake `hermes` script through PATH, and so a missing install produces a
    clear error instead of a bare FileNotFoundError from Popen."""
    return shutil.which("hermes") or "hermes"


def _windowless() -> int:
    """Creation flags for a child that must never flash a console on the user's
    desktop. `CREATE_NO_WINDOW` (never `CREATE_NEW_CONSOLE`/`DETACHED_PROCESS`
    — a console-less parent makes every console program it starts pop its own
    window) plus `CREATE_BREAKAWAY_FROM_JOB` when the OS supports it, which is
    exactly the escape Hermes's own status warning recommends for a gateway that
    must outlive a Job-Object-wrapped parent."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    return flags


def _env(home: Optional[Any] = None) -> dict[str, str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home) if home else str(default_home())
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def run_cli(args: list[str], *, home: Optional[Any] = None, timeout: float = _CLI_TIMEOUT_S) -> str:
    """Run `hermes <args>` windowless and return its combined output.

    stdout+stderr are merged because `hermes gateway status` splits them (the
    process line on stdout, per-platform warnings on stderr) and a caller
    reading only one of them would see a half answer."""
    argv = [hermes_binary(), *args]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_env(home),
            stdin=subprocess.DEVNULL,
            shell=False,
            creationflags=_windowless(),
        )
    except FileNotFoundError as exc:
        raise GatewayError("'hermes' not found on PATH - is Hermes Agent installed?") from exc
    except subprocess.TimeoutExpired as exc:
        raise GatewayError(f"'hermes {' '.join(args)}' timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise GatewayError(f"hermes {' '.join(args)} exited {proc.returncode}: {detail[-800:]}")
    return (proc.stdout or "") + (proc.stderr or "")


# `hermes gateway status`'s own lines (hermes_cli/gateway_windows.py's `status()`,
# and the POSIX branch) - parsed rather than regex-matched on the whole blob so a
# Hermes version that rewords a line degrades to "unknown" instead of lying.
_STATUS_SERVICE_RE = re.compile(r"(Scheduled Task registered|Windows login item installed|Gateway service not installed)\s*:?\s*(.*)$")
_STATUS_PIDS_RE = re.compile(r"Gateway process running \(PID:\s*([^)]*)\)")


def status(home: Optional[Any] = None) -> dict[str, Any]:
    """Parsed `hermes gateway status`, with the raw text kept verbatim.

    `pids`/`running` are what ABP could confirm itself: pids recorded in this
    home's Hermes state files that are still alive AND still identify as a
    Hermes gateway. `reported_pids`/`reported_running` are what Hermes printed.
    Both are returned because they legitimately disagree - Hermes's Windows
    status also scans the process table, which ABP deliberately does not do -
    and silently picking one would hide that from whoever is looking."""
    raw = run_cli(["gateway", "status"], home=home)
    service = None
    for line in raw.splitlines():
        m = _STATUS_SERVICE_RE.search(line)
        if m:
            service = m.group(2).strip() or m.group(1)
            break
    reported: list[int] = []
    m = _STATUS_PIDS_RE.search(raw)
    if m:
        reported = [int(p) for p in re.findall(r"\d+", m.group(1))]
    home_path = Path(home) if home else default_home()
    pids = gateway_pids(home_path)
    return {
        "home": str(home_path),
        "running": bool(pids),
        "pids": pids,
        "reported_running": bool(reported),
        "reported_pids": reported,
        "service": service,
        "raw": raw.strip(),
    }


# `hermes gateway list` prints one row per profile, always as
# `  <marker> <name padded to 24> — <state>` where <marker> is ✓ or ✗ and the
# separator is a spaced em dash (hermes_cli/gateway.py's profile list). Matching
# the marker and the spaced separator exactly - rather than a loose "contains a
# dash" test - is what keeps a profile literally named `ops-2` from being split
# in half.
_LIST_LINE_RE = re.compile(r"^\s*([✓✗])\s+(.*?)\s+—\s+(.+?)\s*$")
_LIST_PID_RE = re.compile(r"\bPID (\d+)")


def list_profiles() -> list[dict[str, Any]]:
    """Parsed `hermes gateway list`: one entry per Hermes profile, with the
    current one flagged. Hermes prints no JSON for this, so the rows are parsed
    and each keeps its original text alongside the fields."""
    raw = run_cli(["gateway", "list"])
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        m = _LIST_LINE_RE.match(line)
        if not m:
            continue
        marker, label, state = m.group(1), m.group(2).strip(), m.group(3).strip()
        current = "(current)" in label
        pid_match = _LIST_PID_RE.search(state)
        out.append({
            "profile": label.replace("(current)", "").strip(),
            "current": current,
            "state": state,
            "running": marker == "✓",
            "pid": int(pid_match.group(1)) if pid_match else None,
            "raw": line.strip(),
        })
    return out


def launcher_script(home: Optional[Any] = None) -> Optional[Path]:
    """The login-item launcher Hermes itself installs for this home, if present.

    `<HERMES_HOME>/gateway-service/Hermes_Gateway.vbs` is what `hermes gateway
    install` drops into the Windows Startup folder: it sets HERMES_HOME /
    PYTHONPATH / HERMES_GATEWAY_DETACHED, then `sh.Run "... gateway run", 0,
    False` - hidden, non-blocking, and started by the shell/OS rather than by
    whatever process happened to spawn it. Reusing it is how ABP starts a
    gateway that survives both ABP and any Job Object wrapping it."""
    home_path = Path(home) if home else default_home()
    for name in ("Hermes_Gateway.vbs", "Hermes_Gateway.cmd"):
        candidate = home_path / "gateway-service" / name
        if candidate.is_file():
            return candidate
    return None


def startup_entry(home: Optional[Any] = None) -> Optional[Path]:
    """The Startup-folder copy of that launcher, which is where Hermes's own
    `hermes gateway status` reports it being installed."""
    appdata = os.environ.get("APPDATA", "").strip()
    if not appdata:
        return None
    candidate = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Hermes_Gateway.vbs"
    return candidate if candidate.is_file() else None


def start(home: Optional[Any] = None) -> dict[str, Any]:
    """Start the gateway so it outlives AgenticBotPlatform (and any Job Object
    ABP itself sits in).

    Preferred path: Hermes's own login-item launcher, run through `cscript`
    windowless. `cscript //nologo <vbs>` executes the same `sh.Run ..., 0,
    False` the Startup folder uses at logon — hidden and detached from this
    process tree. Fallback when no launcher exists (a fresh install, or a
    per-instance home that never had one): spawn `hermes gateway run` directly,
    detached, with the same env the launcher sets and the output going to
    `<HERMES_HOME>/logs/gateway-abp.log`."""
    home_path = Path(home) if home else default_home()
    if gateway_running(home_path):
        return {"ok": True, "already_running": True, "via": "already running", **gateway_state(home_path)}

    script = launcher_script(home_path)
    if script is not None and sys.platform == "win32" and script.suffix.lower() == ".vbs":
        # shutil.which, not a bare "cscript": CreateProcess only ever appends
        # .exe to an extension-less name, so the bare string would miss cscript
        # itself - and a real miss should be a clear error, not a silent
        # fallback to the spawn path with no explanation.
        cscript = shutil.which("cscript")
        if cscript is None:
            raise GatewayError("cscript not found - cannot run Hermes's own gateway launcher (Hermes_Gateway.vbs)")
        argv = [cscript, "//nologo", str(script)]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=_START_TIMEOUT_S, env=_env(home_path), stdin=subprocess.DEVNULL,
                shell=False, creationflags=_windowless(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GatewayError("Hermes's gateway launcher did not return in time") from exc
        if proc.returncode != 0:
            raise GatewayError(f"Hermes's gateway launcher failed ({proc.returncode}): {(proc.stderr or proc.stdout or '').strip()[-800:]}")
        return _await_running(home_path, via=str(script))

    log_dir = home_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "gateway-abp.log", "ab")
    env = _env(home_path)
    env["HERMES_GATEWAY_DETACHED"] = "1"
    try:
        proc = subprocess.Popen(
            [hermes_binary(), "gateway", "run"], stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, cwd=str(home_path), env=env, shell=False, creationflags=_windowless(),
        )
    finally:
        log.close()
    return _await_running(home_path, via=f"spawned pid {proc.pid}")


def _await_running(home_path: Path, *, via: str, timeout: float = 25.0) -> dict[str, Any]:
    """Give the just-started gateway a moment to write its own pid file, then
    report the real state. Never raises on a gateway that has not come up yet:
    the honest answer ("still starting, here's what Hermes says") is more
    useful to an operator than an exception."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if gateway_running(home_path):
            break
        time.sleep(0.25)
    return {"ok": gateway_running(home_path), "already_running": False, "via": via, **gateway_state(home_path)}


def stop(home: Optional[Any] = None) -> dict[str, Any]:
    home_path = Path(home) if home else default_home()
    output = run_cli(["gateway", "stop"], home=home_path)
    return {"ok": True, "output": output.strip(), **gateway_state(home_path)}


def restart(home: Optional[Any] = None) -> dict[str, Any]:
    home_path = Path(home) if home else default_home()
    output = run_cli(["gateway", "restart"], home=home_path, timeout=_START_TIMEOUT_S)
    return {"ok": True, "output": output.strip(), **gateway_state(home_path)}


def log_files(home: Optional[Any] = None) -> list[Path]:
    """`<HERMES_HOME>/logs/gateway*.log`, newest first."""
    log_dir = (Path(home) if home else default_home()) / "logs"
    try:
        return sorted(log_dir.glob("gateway*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def logs(home: Optional[Any] = None, lines: int = 80) -> dict[str, Any]:
    """Tail of this home's gateway log(s) — read straight off disk from
    `<HERMES_HOME>/logs/gateway*.log`, where Hermes's own service writes them
    (including the one bot/hermes_gateway.start falls back to), so there is no
    second place to look and no subprocess to spawn."""
    files = log_files(home)
    lines = max(1, min(int(lines), 5000))
    chunks: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(f"--- {path.name} ---\n" + "\n".join(text.splitlines()[-lines:]))
    return {
        "home": str(home) if home else str(default_home()),
        "files": [str(p) for p in files],
        "tail": "\n".join(chunks),
    }


async def ask(text: str, *, home: Optional[Any] = None, model: Optional[str] = None,
              session_id: Optional[str] = None, timeout_s: float = 300.0) -> dict[str, Any]:
    """One-shot `hermes -z "<text>"` through the existing hermes_cli backend.

    This is how ABP's own channels reach Hermes *while* Hermes's gateway is
    serving Telegram: `hermes -z` is a separate, stateless, per-call process
    that never touches `getUpdates`, so there is no token contention at all. A
    `session_id` (the `desktop_session_key` ABP already persists for Hermes
    instances) resumes that real conversation.

    The HERMES_HOME override is applied to this process's own environment
    (rather than passed through) because HermesCliBackend builds its argv from
    the binary alone and Hermes resolves its home from the environment at
    startup - exactly how hermes_gateway_backend's own `hermes serve` spawn does
    it. It is restored in a finally block."""
    from bot.backends.base import BackendError
    from bot.backends.hermes_cli_backend import HermesCliBackend

    backend = HermesCliBackend(binary=hermes_binary(), model=model)
    previous_home = os.environ.get("HERMES_HOME")
    if home:
        os.environ["HERMES_HOME"] = str(home)
    try:
        context = {"desktop_session_key": session_id} if session_id else {}
        result = await backend.ask(text, context=context, timeout_s=timeout_s)
    except BackendError as exc:
        raise GatewayError(str(exc)) from exc
    finally:
        if home:
            if previous_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previous_home
    return {"text": result.text, "tokens": result.tokens, "session_id": (result.raw or {}).get("desktop_session_key") or session_id}


def overview(home: Optional[Any] = None) -> dict[str, Any]:
    """Everything the dashboard/CLI shows on one Hermes-gateway page: the parsed
    status, the Telegram token ownership (digest only), the profiles and the log
    tail. One call so a page render is one round trip and one `hermes` spawn."""
    home_path = Path(home) if home else default_home()
    token = telegram_token(home_path)
    enabled = telegram_enabled(home_path)
    return {
        "home": str(home_path),
        "state": gateway_state(home_path),
        "telegram": {
            "configured": bool(token),
            "enabled": enabled is not False,
            "enabled_explicit": enabled,
            "token_sha256": token_fingerprint(token) if token else None,
        },
        "launcher": str(launcher_script(home_path)) if launcher_script(home_path) else None,
        "startup_entry": str(startup_entry(home_path)) if startup_entry(home_path) else None,
        "homes": [str(h) for h in homes()],
    }

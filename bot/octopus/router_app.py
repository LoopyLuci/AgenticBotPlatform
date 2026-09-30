"""Running octopus-router itself from ABP: install (clone, build its server and web UI), start, stop, update.

The Router stays its own program in its own repo (Octopus-Security/octopus-router, private: the machine's git
credentials must reach it). ABP only runs it and wires both directions at install:

- the Router's .env gets fresh ROUTER_SECRET and ROUTER_OWNER_TOKEN, the owner, its port on loopback, ABP's address,
  and an ABP integration key (the octopus-router preset) as ABP_DASHBOARD_TOKEN, so its Bot Platform view works at
  once without ABP's root token;
- ABP gets the Router's address and owner token (bot/octopus/router.py), so its Router pane, chat and the provider
  octopus-router work at once.

An existing .env is kept: a reinstall or update never changes the Router's secrets (its stored keys are sealed with
ROUTER_SECRET, so a new one would make them unreadable).
"""
from __future__ import annotations

import getpass
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from bot.octopus import router

REPO = "https://github.com/Octopus-Security/octopus-router.git"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_job: dict[str, Any] = {"state": "idle", "log": [], "started": 0.0, "error": ""}
_lock = threading.Lock()


def _cfg() -> dict:
    try:
        from bot.config import config
        return dict(((config.current or {}).get("octopus") or {}).get("router_app") or {})
    except Exception:  # noqa: BLE001
        return {}


def app_dir() -> Path:
    d = _cfg().get("dir")
    if d:
        return Path(str(d)).expanduser()
    from bot.envfile import PROJECT_ROOT
    return Path(PROJECT_ROOT) / "data" / "octopus" / "octopus-router"


def port() -> int:
    return int(_cfg().get("port") or 3030)


def _pid_file() -> Path:
    return app_dir().parent / "octopus-router.pid"


def _node() -> str | None:
    return shutil.which("node")


def _npm() -> str | None:
    return shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")


def node_version() -> tuple[int, ...]:
    n = _node()
    if not n:
        return ()
    out = subprocess.run([n, "--version"], capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout
    try:
        return tuple(int(x) for x in out.strip().lstrip("v").split(".")[:2])
    except ValueError:
        return ()


def _pid() -> int:
    try:
        return int(_pid_file().read_text().strip())
    except (OSError, ValueError):
        return 0


def _alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid) and "node" in psutil.Process(pid).name().lower()
    except Exception:  # noqa: BLE001
        return False


def status() -> dict:
    d = app_dir()
    commit = ""
    if (d / ".git").is_dir():
        commit = subprocess.run(["git", "-C", str(d), "log", "--oneline", "-1"], capture_output=True, text=True,
                                creationflags=NO_WINDOW).stdout.strip()
    return {"dir": str(d), "installed": (d / "client" / "dist").is_dir() and (d / "node_modules").is_dir(),
            "configured": (d / ".env").is_file(), "running": _alive(_pid()), "pid": _pid() or None, "port": port(),
            "commit": commit, "node": ".".join(map(str, node_version())) or None, "job": dict(_job, log=_job["log"][-40:])}


def _say(line: str) -> None:
    _job["log"].append(line)


def _run(cmd: list[str], cwd: Path, timeout: int = 1800) -> None:
    _say("$ " + " ".join(cmd))
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
                       encoding="utf-8", errors="replace")
    for ln in (p.stdout + p.stderr).splitlines()[-8:]:
        _say("  " + ln)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({p.returncode})")


def _write_env(d: Path) -> bool:
    """A fresh .env on first install only. Returns True if it was written."""
    env = d / ".env"
    if env.is_file():
        return False
    from bot import integrations
    key_id, key = integrations.mint("octopus-router (installed by ABP)", integrations.PRESETS["octopus-router"]["scopes"],
                                    origin=f"http://127.0.0.1:{port()}", preset="octopus-router")
    abp_port = os.environ.get("DASHBOARD_PORT", "8787")
    lines = [
        "# Written by AgenticBotPlatform when it installed the Router. Keep ROUTER_SECRET: it seals the stored keys.",
        f"ROUTER_SECRET={secrets.token_urlsafe(32)}",
        f"ROUTER_OWNER_TOKEN={secrets.token_urlsafe(32)}",
        f"ROUTER_OWNER={getpass.getuser()}",
        f"PORT={port()}",
        "ROUTER_HOST=127.0.0.1",
        f"ABP_URL=http://127.0.0.1:{abp_port}",
        f"ABP_DASHBOARD_TOKEN={key}",
    ]
    ollama = os.environ.get("ABP_OLLAMA_URL") or "http://127.0.0.1:11434"
    lines.append(f"PHANTOM_OLLAMA_URL={ollama}")
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    _say(f"wrote {env} (new secrets; ABP integration key #{key_id} for the Bot Platform view)")
    return True


def _read_env(d: Path) -> dict[str, str]:
    out = {}
    for ln in (d / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in ln and not ln.lstrip().startswith("#"):
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _wire_abp(d: Path) -> None:
    from bot.config import config
    config.set_value(["octopus", "router_url"], f"http://127.0.0.1:{port()}", "octopus-router-app")
    router.set_token(_read_env(d)["ROUTER_OWNER_TOKEN"])
    from bot import integrations
    origin = f"http://127.0.0.1:{port()}"
    for key, have in (("frame_src", integrations.frame_src()), ("frame_ancestors", integrations.frame_ancestors())):
        if origin not in have:
            config.set_value(["integrations", key], [*have, origin], "octopus-router-app")
    _say("ABP now uses this Router (its pane, chat and the provider octopus-router)")


def _install_job(update: bool) -> None:
    d = app_dir()
    try:
        if node_version() < (22,):
            raise RuntimeError("the Router needs Node.js 22 or newer (https://nodejs.org)")
        npm = _npm()
        if not npm:
            raise RuntimeError("npm was not found")
        if (d / ".git").is_dir():
            _run(["git", "-C", str(d), "pull", "--ff-only", "-q"], d.parent)
        else:
            d.parent.mkdir(parents=True, exist_ok=True)
            _run(["git", "clone", "-q", "--depth", "50", REPO, str(d)], d.parent, timeout=600)
        _run([npm, "ci", "--no-audit", "--no-fund"], d)
        _run([npm, "ci", "--no-audit", "--no-fund"], d / "client")
        _run([npm, "run", "build"], d / "client")
        _write_env(d)
        _wire_abp(d)
        if update and _alive(_pid()):
            stop()
            start()
        _job.update(state="done")
    except Exception as e:  # noqa: BLE001 - reported in the job
        _job.update(state="failed", error=str(e))
        _say(f"FAILED: {e}")


def install(update: bool = False) -> dict:
    with _lock:
        if _job["state"] == "running":
            return status()
        _job.update(state="running", log=[], started=time.time(), error="")
        threading.Thread(target=_install_job, args=(update,), daemon=True, name="octopus-router-install").start()
    return status()


def _spawn(cmd: list[str], **kw: Any) -> subprocess.Popen:
    return subprocess.Popen(cmd, **kw)


def start() -> dict:
    d = app_dir()
    if not (d / ".env").is_file():
        raise RuntimeError("install the Router first")
    if _alive(_pid()):
        return status()
    log = open(d.parent / "octopus-router.log", "ab")
    kw: dict[str, Any] = {"creationflags": NO_WINDOW | (0x00000200 if os.name == "nt" else 0)}  # new process group
    if os.name != "nt":
        kw = {"start_new_session": True}
    p = _spawn([_node() or "node", "--env-file=.env", "server/index.js"], cwd=str(d), stdout=log,
               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
    _pid_file().write_text(str(p.pid))
    for _ in range(60):
        try:
            router.request("GET", "/api/build", auth=False, timeout=2)
            break
        except router.RouterError:
            time.sleep(0.5)
    return status()


def stop() -> dict:
    pid = _pid()
    if _alive(pid):
        try:
            import psutil
            proc = psutil.Process(pid)
            for c in proc.children(recursive=True):
                c.kill()
            proc.terminate()
            proc.wait(10)
        except Exception:  # noqa: BLE001
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    _pid_file().unlink(missing_ok=True)
    return status()


if __name__ == "__main__":   # python -m bot.octopus.router_app install|start|stop|status
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "install":
        _job.update(state="running", log=[], started=time.time())
        _install_job(False)
        print("\n".join(_job["log"]))
    else:
        print({"start": start, "stop": stop}.get(cmd, status)())

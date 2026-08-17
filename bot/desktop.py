"""Process control and MCP config management for Claude Desktop.

Deliberately outside the backend router: these are direct Windows/file
operations on the Desktop app itself (start/stop/restart, editing
claude_desktop_config.json), not "ask Claude something". Destructive
actions are flagged for the caller to gate behind a confirmation step —
this module does the action, the caller (handlers.py / dashboard) decides
whether to ask first.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from bot import db, envfile
from bot.config import config

logger = logging.getLogger("bot.desktop")

PROCESS_NAME = "Claude.exe"


def _mcp_config_path() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Claude" / "claude_desktop_config.json"


def _mcp_log_dir() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Claude" / "logs"


def _find_msix_claude_exe() -> Optional[str]:
    """Claude Desktop can also be installed as an MSIX/Store package rather
    than the legacy Squirrel installer the AnthropicClaude glob below
    covers — its install folder is versioned
    (...\\WindowsApps\\Claude_<version>_x64_...) and moves on every update,
    so this re-resolves fresh on every call rather than ever being cached
    or saved into .env's CLAUDE_DESKTOP_EXE, which is exactly why that
    field stays optional: a static path would go stale the next time
    Claude Desktop auto-updates, while this keeps working across updates
    with zero maintenance."""
    if platform.system() != "Windows":
        return None
    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                "(Get-AppxPackage -Name 'Claude' -ErrorAction SilentlyContinue).InstallLocation",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        install_dir = result.stdout.strip()
    except Exception:
        return None
    if not install_dir or not Path(install_dir).exists():
        return None
    matches = glob.glob(str(Path(install_dir) / "**" / "claude.exe"), recursive=True)
    return matches[0] if matches else None


def find_cli_path(binary: str = "claude") -> Optional[str]:
    """Resolves the Claude Code CLI: PATH first (a real global npm
    install), then Claude Desktop's own bundled copy at
    %APPDATA%\\Claude\\claude-code\\<version>\\claude.exe. Desktop ships
    that copy for its own local-agent-mode use and never puts it on
    PATH, so without this fallback the `cli` backend looks broken even
    on a machine where a working claude.exe is sitting right there —
    same shape of fix as find_exe_path()'s MSIX fallback below."""
    found = shutil.which(binary)
    if found:
        return found
    if platform.system() != "Windows":
        return None
    appdata = os.environ.get("APPDATA", "")
    candidates = sorted(
        Path(appdata, "Claude", "claude-code").glob("*/claude.exe"),
        key=lambda p: p.parent.name,
        reverse=True,
    )
    return str(candidates[0]) if candidates else None


def install_cli(actor: str = "dashboard") -> dict[str, Any]:
    """Installs or updates the standalone Claude Code CLI via
    `npm install -g @anthropic-ai/claude-code`. Not strictly required if
    find_cli_path() already resolves Desktop's bundled copy — mainly
    useful on a machine without Desktop installed, or to get a newer CLI
    version than Desktop currently bundles. Requires npm on PATH."""
    npm = shutil.which("npm")
    if not npm:
        return {"ok": False, "output": "npm not found on PATH — install Node.js from nodejs.org, then retry."}
    try:
        result = subprocess.run(
            [npm, "install", "-g", "@anthropic-ai/claude-code"],
            capture_output=True,
            text=True,
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "npm install timed out after 180s"}
    except Exception as exc:
        return {"ok": False, "output": str(exc)}
    ok = result.returncode == 0
    output = ((result.stdout or "") + (result.stderr or ""))[-4000:]
    db.log_audit(actor=actor, action="cli_install", detail=f"npm install -g @anthropic-ai/claude-code -> {'ok' if ok else 'failed'}")
    return {"ok": ok, "output": output}


def find_exe_path() -> Optional[str]:
    cfg_path = (config.current.get("desktop") or {}).get("exe_path")
    if cfg_path:
        return cfg_path
    env_path = os.environ.get("CLAUDE_DESKTOP_EXE")
    if env_path:
        return env_path
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = glob.glob(str(Path(local) / "AnthropicClaude" / "**" / PROCESS_NAME), recursive=True)
    if candidates:
        return candidates[0]
    return _find_msix_claude_exe()


def get_process() -> Optional["psutil.Process"]:  # noqa: F821
    import psutil

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == PROCESS_NAME.lower():
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def status() -> dict[str, Any]:
    proc = get_process()
    if not proc:
        return {"running": False}
    try:
        with proc.oneshot():
            return {
                "running": True,
                "pid": proc.pid,
                "cpu_percent": proc.cpu_percent(interval=0.1),
                "memory_mb": round(proc.memory_info().rss / (1024 * 1024), 1),
                "started_at": proc.create_time(),
            }
    except Exception:
        return {"running": True, "pid": proc.pid}


def start() -> bool:
    if get_process():
        return True
    exe = find_exe_path()
    if not exe or not Path(exe).exists():
        raise FileNotFoundError(
            "could not locate Claude.exe — set desktop.exe_path in config/backends.yaml"
        )
    subprocess.Popen([exe], close_fds=True)
    db.log_connection_event(component="desktop", event="start")
    db.log_audit(actor="system", action="desktop_start", detail=exe)
    return True


def stop(grace_s: float = 5.0) -> bool:
    proc = get_process()
    if not proc:
        return True
    try:
        proc.terminate()
        proc.wait(timeout=grace_s)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    db.log_connection_event(component="desktop", event="stop")
    db.log_audit(actor="system", action="desktop_stop")
    return True


def restart() -> bool:
    stop()
    time.sleep(1.0)
    start()
    db.log_audit(actor="system", action="desktop_restart")
    return True


# --------------------------------------------------------- MCP servers ----

def load_mcp_config() -> dict[str, Any]:
    path = _mcp_config_path()
    if not path.exists():
        return {"mcpServers": {}, "mcpServers_disabled": {}}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("mcpServers", {})
    data.setdefault("mcpServers_disabled", {})
    return data


def _save_mcp_config(data: dict[str, Any]) -> None:
    path = _mcp_config_path()
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def list_mcp_servers() -> list[dict[str, Any]]:
    data = load_mcp_config()
    out = []
    for name, cfg in data.get("mcpServers", {}).items():
        out.append({"name": name, "enabled": True, "command": cfg.get("command", "")})
    for name, cfg in data.get("mcpServers_disabled", {}).items():
        out.append({"name": name, "enabled": False, "command": cfg.get("command", "")})
    return out


def enable_mcp(name: str) -> bool:
    data = load_mcp_config()
    if name in data["mcpServers"]:
        return True
    if name not in data["mcpServers_disabled"]:
        raise KeyError(f"unknown MCP server {name!r}")
    data["mcpServers"][name] = data["mcpServers_disabled"].pop(name)
    _save_mcp_config(data)
    db.log_mcp_event(server=name, event="enabled")
    db.log_audit(actor="dashboard", action="mcp_enable", detail=name)
    return True


def disable_mcp(name: str) -> bool:
    data = load_mcp_config()
    if name in data["mcpServers_disabled"]:
        return True
    if name not in data["mcpServers"]:
        raise KeyError(f"unknown MCP server {name!r}")
    data["mcpServers_disabled"][name] = data["mcpServers"].pop(name)
    _save_mcp_config(data)
    db.log_mcp_event(server=name, event="disabled")
    db.log_audit(actor="dashboard", action="mcp_disable", detail=name)
    return True


def register_self_mcp(actor: str = "dashboard") -> dict[str, Any]:
    """Adds this app's own control server (bot/mcp_server.py) to Claude
    Desktop's claude_desktop_config.json under the name "bot-server",
    pointing at the same venv python already running this process. This is
    what closes the loop the rest of the app is built around: the Telegram
    bot can drive Claude Desktop (ui_backend.py), and now Claude Desktop (or
    Claude Code, via the equivalent `claude mcp add`) can drive this app
    back, over MCP instead of Telegram/HTTP/GUI.

    Idempotent — re-running just overwrites the one "bot-server" entry with
    the current python path and token, so it stays correct after a token
    rotation or a venv rebuild. Also drops a stale "telegram-bot-server"
    entry left over from before the project was renamed. Sets PYTHONPATH
    explicitly rather than relying on an MCP client honoring a "cwd" key
    (not all do), since `-m bot.mcp_server` needs the project root
    importable regardless of what working directory the client launches it
    from.
    """
    python = sys.executable
    project_root = str(envfile.PROJECT_ROOT)
    token = os.environ.get("DASHBOARD_TOKEN", "")
    env_vars = {"PYTHONPATH": project_root}
    if token:
        env_vars["DASHBOARD_TOKEN"] = token
    entry = {
        "command": python,
        "args": ["-m", "bot.mcp_server"],
        "cwd": project_root,
        "env": env_vars,
    }
    data = load_mcp_config()
    data.setdefault("mcpServers", {})
    data["mcpServers"]["bot-server"] = entry
    data["mcpServers"].pop("telegram-bot-server", None)
    data.get("mcpServers_disabled", {}).pop("bot-server", None)
    data.get("mcpServers_disabled", {}).pop("telegram-bot-server", None)
    _save_mcp_config(data)
    db.log_audit(actor=actor, action="mcp_self_register", detail=f"registered bot-server -> {python}")
    return {"name": "bot-server", "command": python, "cwd": project_root}


def tail_mcp_log(name: str, lines: int = 50) -> list[str]:
    log_dir = _mcp_log_dir()
    matches = sorted(log_dir.glob(f"*{name}*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        return [f"no log file found for MCP server {name!r} in {log_dir}"]
    with open(matches[0], "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    return [ln.rstrip("\n") for ln in all_lines[-lines:]]

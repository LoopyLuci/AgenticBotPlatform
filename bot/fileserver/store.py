"""Where the file server keeps its state: `<data>/fileserver/` (ABP_FILESERVER_DIR overrides it)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

_lock = threading.RLock()


class FsError(Exception):
    """A problem a person can act on; shown as is."""


def root() -> Path:
    explicit = os.environ.get("ABP_FILESERVER_DIR", "").strip()
    if explicit:
        path = Path(explicit)
    else:
        from bot.envfile import PROJECT_ROOT
        path = PROJECT_ROOT / "data" / "fileserver"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load(name: str, default: Any) -> Any:
    path = root() / f"{name}.json"
    with _lock:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except ValueError as e:
            raise FsError(f"{path} is not valid JSON ({e})") from e


def save(name: str, value: Any) -> None:
    path = root() / f"{name}.json"
    with _lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def update(name: str, default: Any, fn) -> Any:
    with _lock:
        value = load(name, default)
        out = fn(value)
        save(name, value)
        return out


def db(name: str) -> sqlite3.Connection:
    """A SQLite database in the state folder (WAL, one connection per call site; close it when done)."""
    con = sqlite3.connect(root() / f"{name}.db", timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con

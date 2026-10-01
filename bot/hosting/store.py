"""Where hosting keeps its state: `<data>/hosting/` (ABP_HOSTING_DIR overrides it), small JSON files written atomically."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_lock = threading.RLock()


class HostingError(Exception):
    """A problem a person can act on (a bad name, a provider's refusal, a missing tool); shown as is."""


def root() -> Path:
    explicit = os.environ.get("ABP_HOSTING_DIR", "").strip()
    if explicit:
        path = Path(explicit)
    else:
        from bot.envfile import PROJECT_ROOT
        path = PROJECT_ROOT / "data" / "hosting"
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
            raise HostingError(f"{path} is not valid JSON ({e}); fix or remove it") from e


def save(name: str, value: Any) -> None:
    path = root() / f"{name}.json"
    with _lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def update(name: str, default: Any, fn) -> Any:
    """Read, change and write back under one lock; returns what fn returns."""
    with _lock:
        value = load(name, default)
        out = fn(value)
        save(name, value)
        return out

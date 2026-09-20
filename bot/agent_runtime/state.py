"""Where the agent runtime keeps its own small pieces of state on disk
(todo lists, background-job logs, browser profiles later). One place, one
override, so tests and embedded hosts can point it somewhere private.

`ABP_AGENT_STATE_DIR` overrides; otherwise `<state root>/data/agent`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def state_dir(*parts: str) -> Path:
    explicit = os.environ.get("ABP_AGENT_STATE_DIR", "").strip()
    if explicit:
        base = Path(explicit)
    else:
        from bot.envfile import PROJECT_ROOT

        base = PROJECT_ROOT / "data" / "agent"
    path = base.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(value: str, limit: int = 80) -> str:
    """A session or job id made safe to use as a file name."""
    return _SAFE.sub("_", value)[:limit] or "default"

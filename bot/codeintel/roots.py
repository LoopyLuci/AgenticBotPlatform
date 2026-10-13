"""Which folders the HTTP API and the MCP tools may index and read: ABP's own code folder, and the folders listed in
`native_agent.codeintel.roots`. Anything else is refused, so a dashboard token cannot be used to read arbitrary
source trees. (The native agent's tools work in the agent's own workspace and do not go through here.)"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


class RootNotAllowed(PermissionError):
    pass


def allowed_roots() -> list[Path]:
    from bot.envfile import CODE_ROOT, PROJECT_ROOT

    roots = [Path(CODE_ROOT).resolve(), Path(PROJECT_ROOT).resolve()]
    try:
        from bot.config import config

        extra = ((config.current.get("native_agent") or {}).get("codeintel") or {}).get("roots") or []
        roots += [Path(str(p)).expanduser().resolve() for p in extra if str(p).strip()]
    except Exception:  # noqa: BLE001 - a config problem leaves the built-in roots
        pass
    return roots


def resolve_root(root: Optional[str]) -> Path:
    """The folder to work in: the default (ABP's code) or `root` when it is inside an allowed folder."""
    allowed = allowed_roots()
    if not (root or "").strip():
        return allowed[0]
    path = Path(str(root)).expanduser().resolve()
    for base in allowed:
        try:
            path.relative_to(base)
            break
        except ValueError:
            continue
    else:
        raise RootNotAllowed(f"{path} is not an allowed code root; list it under native_agent.codeintel.roots in the config")
    if not path.is_dir():
        raise RootNotAllowed(f"{path} is not a folder")
    return path

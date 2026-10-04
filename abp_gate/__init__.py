"""abp_gate - the stable front door in front of ABP.

One process that owns the public port(s) everything already talks to (8787
today) and reverse-proxies to whichever ABP instance is currently active, so:

  * ABP stays usable with no console window and no GUI attached, and closing
    the desktop app never takes it offline;
  * an agent can run a sandboxed instance of ABP against its own state and its
    own worktree, on its own port, without touching the running one;
  * new code is swapped in with no downtime, and rolled back on its own if the
    new instance does not come up healthy or does not take the leader lease
    (bot/lease.py).

It is small on purpose. Nothing in here imports bot.*, so it keeps working
while the code it fronts is being replaced underneath it, and a bug in ABP's
business logic can't take the front door down with it. The only bot.* import is
a deliberate, late one to read the dashboard token (control.py), and that is a
plain .env read.

    paths.py      where files go, which ports we own
    registry.py   the instance registry (name, code root, data root, port, pid,
                  role, health) and its atomic on-disk form
    procs.py      windowless start / tree stop / health check / log tail
    proxy.py      the ASGI reverse proxy (HTTP incl. SSE + chunked, and
                  WebSockets) and the public listeners
    manager.py    the orchestration: start, swap, rollback, sandbox, stop
    control.py    the localhost control API, authenticated with ABP's own
                  dashboard token
    __main__.py   `python -m abp_gate`
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
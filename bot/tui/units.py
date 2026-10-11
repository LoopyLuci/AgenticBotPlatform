"""Which TUI screens belong to a unit of the runtime (docs/runtime.md), and which of those units are live.

The mapping mirrors the web dashboard's `data-unit` marks (bot/dashboard/static/dashboard.html); a screen that has no
unit (bots, providers, sessions, ...) is always offered. `UnitFollower` reads GET /api/ui/manifest and then polls
GET /api/runtime/events?since=N, the same fallback the web UI's module host uses when it has no websocket. A server
without /api/ui/manifest (404, an older build) leaves everything shown, as before.
"""
from __future__ import annotations

from typing import Optional

from bot.tui.client import ApiError

# BotListScreen action name -> unit id (the web UI's data-unit)
ACTION_UNITS = {
    "hosting": "hosting",
    "storage": "storage",
    "localai": "localai",
}


def live_units(manifest: dict) -> set:
    """Unit ids with a live panel, computed as module-host.js does."""
    out = set()
    for panel in manifest.get("panels") or []:
        if panel.get("kind") == "script":
            continue
        key = panel.get("unit") or panel.get("section") or panel.get("id")
        if key:
            out.add(key)
    return out


class UnitFollower:
    def __init__(self) -> None:
        self.live: Optional[set] = None      # None: not known (no manifest yet / older server): show everything
        self.generation = -1
        self.available: Optional[bool] = None

    def visible(self, unit: Optional[str]) -> bool:
        return unit is None or self.live is None or unit in self.live

    async def refresh(self, client) -> bool:
        """Re-read the manifest; True when what is live changed."""
        try:
            manifest = await client.ui_manifest()
        except ApiError as exc:
            if exc.status_code == 404:
                self.available = False
                changed = self.live is not None
                self.live = None
                return changed
            return False
        except Exception:  # noqa: BLE001 - unreachable server: keep what is shown
            return False
        self.available = True
        generation = int(manifest.get("generation") or 0)
        self.generation = max(self.generation, generation)
        # generation 0: the runtime has not booted (still starting, or its boot failed) - nothing is known yet, so
        # everything stays shown rather than hiding screens of units that are about to load
        new = live_units(manifest) if generation > 0 else None
        changed = new != self.live
        self.live = new
        return changed

    async def poll(self, client) -> bool:
        """One polling tick: refresh the manifest when the runtime committed something. True when visibility changed."""
        if self.available is False:
            return False
        if self.available is None:
            return await self.refresh(client)
        try:
            data = await client.runtime_events(self.generation)
        except Exception:  # noqa: BLE001
            return False
        generation = int(data.get("generation") or 0)
        if data.get("events") or generation > self.generation:
            return await self.refresh(client)
        return False

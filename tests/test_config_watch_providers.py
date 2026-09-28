"""config/providers.yaml is watched like backends.yaml, so a provider added outside the app (abp_import, an edit by
hand) is live without a restart; the watcher waits quietly while an optional file does not exist."""
from __future__ import annotations

import asyncio

from bot.config import ConfigManager


def test_a_watched_file_that_appears_and_changes_is_reloaded(tmp_path):
    path = tmp_path / "providers.yaml"
    mgr = ConfigManager(path=path, missing_ok=True)     # as bot/providers.py builds it
    assert mgr.missing_ok and mgr.current == {}

    async def scenario():
        task = asyncio.create_task(mgr.watch_forever())
        await asyncio.sleep(0.2)
        path.write_text("providers:\n  one: {base_url: http://a/v1}\n", encoding="utf-8")
        for _ in range(100):                        # the missing-file wait polls every 5 s
            if "one" in (mgr.current.get("providers") or {}):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)                     # let awatch start on the new file
        path.write_text("providers:\n  one: {base_url: http://a/v1}\n  two: {base_url: http://b/v1}\n", encoding="utf-8")
        for _ in range(100):
            if "two" in (mgr.current.get("providers") or {}):
                break
            await asyncio.sleep(0.1)
        task.cancel()
        return mgr.current.get("providers") or {}

    assert set(asyncio.run(scenario())) == {"one", "two"}

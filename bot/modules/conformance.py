"""Checking a module against the module contract (docs/modules/ROADMAP.md §2), for tests and for the Modules page.

    check(module_id, start=True) -> {"module", "ok", "checks": [{"check", "ok", "detail"}]}

It starts the hub if it is not running (and stops it again afterwards if it started it), then checks that the hub
answers health, refuses callers without the token, lists operations with ids and schemas, runs a read-only
operation, and stops when asked.
"""
from __future__ import annotations

from typing import Any

import httpx

from bot.modules import client, harness, registry
from bot.modules.client import ModuleError


def check(mid: str, *, start: bool = True) -> dict:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    try:
        m = registry.get(mid)
    except registry.UnknownModule as e:
        add("manifest", False, str(e.args[0]))
        return {"module": mid, "ok": False, "checks": checks}
    err = registry.manifest_errors().get(m.id)
    add("manifest", not err, err or m.source)
    d = harness.checkout_dir(m)
    if not add("checkout", registry.is_checkout(m, d), str(d)):
        return {"module": m.id, "ok": False, "checks": checks}
    if m.adapter:
        add("hub", True, f"through the {m.adapter} adapter (its own tests cover its hub)")
        return {"module": m.id, "ok": all(c["ok"] for c in checks), "checks": checks}
    if not add("hub declared", m.hub is not None, "" if m.hub else "no [hub] in the manifest yet"):
        return {"module": m.id, "ok": False, "checks": checks}

    started_here = False
    hub = client.find(m)
    if hub is None and start:
        try:
            harness.start_hub(m.id)
            started_here = True
        except ModuleError as e:
            add("hub starts", False, str(e))
            return {"module": m.id, "ok": False, "checks": checks}
        hub = client.find(m)
    if not add("hub answers health", hub is not None, hub.url if hub else "not running"):
        return {"module": m.id, "ok": False, "checks": checks}
    try:
        r = httpx.get(hub.url + m.hub.api_base + m.hub.operations, timeout=5)
        add("refuses callers without the token", r.status_code in (401, 403), f"HTTP {r.status_code}")
    except httpx.HTTPError as e:
        add("refuses callers without the token", False, str(e))
    try:
        ops = client.operations(m, refresh=True)
        add("lists operations", bool(ops), f"{len(ops)} operations")
        bad = [o["id"] for o in ops if not isinstance(o.get("input_schema"), dict)]
        add("every operation has an input schema", not bad, ", ".join(bad[:5]))
        reads = [o for o in ops if not o["mutating"] and not (o["input_schema"].get("required") or [])]
        if reads:
            try:
                client.call(m, reads[0]["id"], {}, timeout=30)
                add("runs a read-only operation", True, reads[0]["id"])
            except ModuleError as e:
                add("runs a read-only operation", False, f"{reads[0]['id']}: {e}")
    except ModuleError as e:
        add("lists operations", False, str(e))
    if started_here:
        res = harness.stop_hub(m.id)
        add("stops when asked", not res.get("running") and not res.get("killed"),
            "had to be killed" if res.get("killed") else "")
    return {"module": m.id, "ok": all(c["ok"] for c in checks), "checks": checks}

"""The agent's power tools.

    power_status       whether this machine is being kept awake and why, its network cards, the machines it can wake
    power_keep_awake   keep this machine awake for a while (or release it), or change the mode (asks first)
    power_wake         wake a machine with Wake-on-LAN, optionally through a linked server on its network (asks first)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


def _out(v: Any) -> str:
    return json.dumps(v, indent=1, default=str)[:12000]


def register_tools() -> None:
    from bot import power
    from bot.agent_runtime import toolspec

    async def remote(inp, method, path, body=None):
        from bot import peers
        try:
            return _out(await peers.on_machine(str(inp["machine"]), method, path, body))
        except peers.PeerError as e:
            return f"Error: {e}"

    async def status(inp, **_):
        if inp.get("machine"):
            return await remote(inp, "GET", "/api/power/info" if inp.get("cards") else "/api/power/status")
        state = power.keeper.state()
        if inp.get("cards"):
            state["cards"] = (await asyncio.to_thread(power.info))["cards"]
        return _out(state)

    async def keep_awake(inp, **_):
        if inp.get("machine"):
            if inp.get("mode"):
                return await remote(inp, "PUT", "/api/power/settings", {"keep_awake": inp["mode"]})
            if inp.get("release"):
                return await remote(inp, "POST", "/api/power/release", {"key": inp.get("key") or "agent"})
            return await remote(inp, "POST", "/api/power/hold", {"key": inp.get("key") or "agent", "by": "a linked server's agent",
                                                              "reason": inp.get("reason") or "an agent asked",
                                                              "minutes": inp.get("minutes", 60)})
        try:
            if inp.get("mode"):
                return _out(await asyncio.to_thread(power.save_settings, keep_awake=str(inp["mode"])))
            if inp.get("release"):
                return _out(power.keeper.release(str(inp.get("key") or "agent")))
            return _out(power.keeper.hold(str(inp.get("key") or "agent"), str(inp.get("reason") or "an agent asked"),
                                          float(inp.get("minutes", 60)), by="agent"))
        except ValueError as e:
            return f"Error: {e}"

    async def wake(inp, **_):
        from bot import peers
        try:
            if inp.get("learn"):
                return _out(await power.learn_peer(inp["learn"]))
            if inp.get("via"):
                row = peers.find_peer(inp["via"])
                target = power.settings()["wake"].get(str(inp.get("target") or ""), {})
                mac = inp.get("mac") or target.get("mac")
                if not mac:
                    return "Error: no MAC for that target (learn it first, or give mac)"
                return _out(await peers.proxy(row, "POST", "/api/power/wake", {"mac": mac, "broadcast": inp.get("broadcast") or target.get("broadcast")}))
            if inp.get("target"):
                return _out(await asyncio.to_thread(power.wake_target, str(inp["target"])))
            if inp.get("mac"):
                return _out(await asyncio.to_thread(power.wake, str(inp["mac"]), str(inp.get("broadcast") or "255.255.255.255")))
            return "Error: give target (a learned machine), mac, or learn (a linked server's name)"
        except (ValueError, peers.PeerError) as e:
            return f"Error: {e}"

    MACHINE = {"type": "string", "description": "a linked server's name: do this on that machine (it must allow power)"}

    def reg(name, description, props, handler, *, permission, read_only):
        toolspec.register({"name": name, "description": description,
                           "input_schema": {"type": "object", "properties": props, "required": []}},
                          toolspec.ToolSpec(name, permission, read_only=read_only, concurrency_safe=read_only,
                                            origin="registered"), handler)

    reg("power_status", "Whether this machine is being kept awake and why (mode, holds, recent activity), and the machines "
        "it knows how to wake. cards=true adds this machine's network cards (MAC, address, whether wake is enabled).",
        {"cards": {"type": "boolean"}, "machine": MACHINE}, status, permission="read", read_only=True)
    reg("power_keep_awake", "Keep this machine from sleeping for a number of minutes (0 = until released) with a reason, "
        "release a hold (release=true), or change the mode (mode: off, always, while_busy).",
        {"minutes": {"type": "number"}, "reason": {"type": "string"}, "key": {"type": "string"},
         "release": {"type": "boolean"}, "mode": {"type": "string", "enum": ["off", "always", "while_busy"]},
         "machine": MACHINE},
        keep_awake, permission="config", read_only=False)
    reg("power_wake", "Wake a sleeping machine with Wake-on-LAN: target (a machine learned before), or mac (+ broadcast). "
        "via sends the packet from a linked server on the target's network (needed when the target is on another "
        "subnet). learn (a linked server's name) records its network cards so it can be woken later.",
        {"target": {"type": "string"}, "mac": {"type": "string"}, "broadcast": {"type": "string"},
         "via": {"type": "string"}, "learn": {"type": "string"}}, wake, permission="external", read_only=False)


register_tools()

"""Paired phones as nodes the agent can ask things of (roadmap P7): a photo, the screen, the location.

A *node* is a paired device (the same api key the app already uses) that has told the server what it can do. The
agent asks for something with the `node_invoke` tool; the server queues a command for that device, the device picks it
up (it long-polls `GET /api/nodes/poll`, and a push message wakes it if it is asleep), does it, and posts the result to
`POST /api/nodes/result`. Nothing is pushed into a phone without the phone asking for it, and nothing runs that the phone
did not declare.

**Consent is per capability and starts at `deny`.** A person sets each capability for each device to:

    deny     the default; the agent cannot use it
    device   the phone asks its own user every time ("The agent wants to take a photo - allow?") and answers for them
    allow    no prompt on the phone (the server-side permission rules and approvals still apply)

Consent is changed through the dashboard token (`PUT /api/nodes/{device}/consent`), never through a device's own key and
never by the agent.

Capabilities: camera.snap, screen.capture, location.get, clipboard.read, notify.show. A result that is an image is
stored as an attachment and the agent is given its path; text results come back as text. Every result is treated as
untrusted content (a clipboard can hold anything) and taints the session, like a web page.

**Only the server half exists.** There is a small reference node in `abp_node/` that answers with canned data for
development and tests; the **Android app does not implement this protocol yet**, so no real phone has ever answered a
command. The wire format below is the contract the app will need to implement.

    GET  /api/nodes/poll?wait=25        -> {"commands": [{"id", "capability", "args"}]}   (long poll; empty after `wait` seconds)
    POST /api/nodes/result              {"id", "ok": bool, "data": {...} | "error": "..."}
                                        data may hold "text", or "image_b64" + "mime" for pictures
    POST /api/nodes/register            {"name", "capabilities": [...]}
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from bot import db

logger = logging.getLogger("bot.nodes")

CAPABILITIES: dict[str, str] = {
    "camera.snap": "take a photo with the phone's camera",
    "screen.capture": "capture what is on the phone's screen",
    "location.get": "read the phone's current location",
    "clipboard.read": "read the phone's clipboard",
    "notify.show": "show a notification on the phone",
}
MODES = ("deny", "device", "allow")
MAX_RESULT_BYTES = 6_000_000
ONLINE_WINDOW_S = 120.0
DEFAULT_TIMEOUT_S = 45.0


class NodeError(Exception):
    pass


@dataclass
class _Command:
    id: str
    device_id: int
    capability: str
    args: dict
    future: asyncio.Future = field(repr=False, default=None)


_queues: dict[int, list[_Command]] = {}
_waiting: dict[int, list[asyncio.Event]] = {}     # one event per poll currently waiting, so nothing outlives its event loop
_pending: dict[str, _Command] = {}
_last_seen: dict[int, float] = {}


def _conn():
    conn = db.get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            device_id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', capabilities TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS node_consent (
            device_id INTEGER NOT NULL, capability TEXT NOT NULL, mode TEXT NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY (device_id, capability)
        );
    """)
    return conn


# ---- registry and consent ---------------------------------------------------------------------------------------
def register(device_id: int, name: str, capabilities: list) -> list[str]:
    """Record what a device says it can do (unknown capabilities are dropped). Returns the ones kept."""
    kept = [c for c in dict.fromkeys(str(c) for c in capabilities or []) if c in CAPABILITIES]
    with db._lock:
        conn = _conn()
        conn.execute("INSERT INTO nodes (device_id, name, capabilities, updated_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(device_id) DO UPDATE SET name=excluded.name, capabilities=excluded.capabilities, updated_at=excluded.updated_at",
                     (device_id, (name or "")[:60], json.dumps(kept), time.time()))
        conn.commit()
    _last_seen[device_id] = time.time()
    return kept


def set_consent(device_id: int, capability: str, mode: str) -> None:
    if capability not in CAPABILITIES:
        raise NodeError(f"unknown capability {capability!r}; known: {', '.join(CAPABILITIES)}")
    if mode not in MODES:
        raise NodeError(f"mode must be one of {', '.join(MODES)}")
    with db._lock:
        conn = _conn()
        conn.execute("INSERT INTO node_consent (device_id, capability, mode, updated_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(device_id, capability) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at",
                     (device_id, capability, mode, time.time()))
        conn.commit()
    db.log_audit(actor="dashboard", action="node_consent", detail=f"device {device_id}: {capability} -> {mode}")


def consent(device_id: int, capability: str) -> str:
    row = _conn().execute("SELECT mode FROM node_consent WHERE device_id=? AND capability=?", (device_id, capability)).fetchone()
    return row["mode"] if row else "deny"


def listing() -> list[dict]:
    out = []
    for row in _conn().execute("SELECT * FROM nodes ORDER BY device_id").fetchall():
        caps = json.loads(row["capabilities"])
        out.append({"device_id": row["device_id"], "name": row["name"], "online": online(row["device_id"]),
                    "capabilities": {c: consent(row["device_id"], c) for c in caps}})
    return out


def resolve(ref: Any) -> int:
    """A device id or a name (case-insensitive) -> the device id of a registered node."""
    nodes = _conn().execute("SELECT device_id, name FROM nodes").fetchall()
    text = str(ref).strip()
    for n in nodes:
        if text.isdigit() and n["device_id"] == int(text):
            return n["device_id"]
    named = [n["device_id"] for n in nodes if n["name"].lower() == text.lower()]
    if len(named) == 1:
        return named[0]
    raise NodeError(f"no single node matches {ref!r} (node_list shows them)" if named or nodes else "no phone has registered as a node yet")


def online(device_id: int) -> bool:
    return time.time() - _last_seen.get(device_id, 0.0) < ONLINE_WINDOW_S


# ---- the device side of the protocol -------------------------------------------------------------------------------
async def poll(device_id: int, wait_s: float = 25.0) -> list[dict]:
    """Commands waiting for `device_id`; waits up to `wait_s` for one to arrive."""
    _last_seen[device_id] = time.time()
    if not _queues.get(device_id):
        event = asyncio.Event()
        _waiting.setdefault(device_id, []).append(event)
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.0, min(wait_s, 55.0)))
        except asyncio.TimeoutError:
            pass
        finally:
            waiting = _waiting.get(device_id, [])
            if event in waiting:
                waiting.remove(event)
    _last_seen[device_id] = time.time()
    cmds, _queues[device_id] = _queues.get(device_id, []), []
    return [{"id": c.id, "capability": c.capability, "args": c.args} for c in cmds]


def submit_result(device_id: int, command_id: str, ok: bool, data: Optional[dict] = None, error: str = "") -> bool:
    """A device's answer. False if the command is unknown, already answered, or belongs to another device."""
    cmd = _pending.get(command_id)
    if cmd is None or cmd.device_id != device_id or cmd.future.done():
        return False
    if ok:
        size = len(json.dumps(data or {}))
        if size > MAX_RESULT_BYTES:
            cmd.future.set_exception(NodeError("the device's answer was too large to accept"))
        else:
            cmd.future.set_result(data or {})
    else:
        cmd.future.set_exception(NodeError(str(error or "the device declined or failed")[:300]))
    return True


# ---- the agent side ----------------------------------------------------------------------------------------------------
async def invoke(device_id: int, capability: str, args: Optional[dict] = None, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    node = _conn().execute("SELECT * FROM nodes WHERE device_id=?", (device_id,)).fetchone()
    if node is None or capability not in json.loads(node["capabilities"]):
        raise NodeError(f"that device does not offer {capability}")
    mode = consent(device_id, capability)
    if mode == "deny":
        raise NodeError(f"{capability} is not allowed on this device (its owner has not enabled it; the dashboard sets consent per capability)")
    if not online(device_id):
        raise NodeError("that device is not connected right now")
    cmd = _Command(uuid.uuid4().hex, device_id, capability, {**(args or {}), "consent": mode})
    cmd.future = asyncio.get_running_loop().create_future()
    _pending[cmd.id] = cmd
    _queues.setdefault(device_id, []).append(cmd)
    waiting = _waiting.get(device_id) or []
    for event in waiting:
        event.set()                                   # a phone that is polling right now picks it up at once
    if not waiting:
        asyncio.create_task(_wake(device_id))         # otherwise nudge it with a push message
    db.log_audit(actor="agent", action="node_invoke", detail=f"device {device_id}: {capability} ({mode})")
    try:
        return await asyncio.wait_for(cmd.future, timeout=timeout_s)
    except asyncio.TimeoutError:
        raise NodeError("the device did not answer in time")
    finally:
        _pending.pop(cmd.id, None)
        _queues[device_id] = [c for c in _queues.get(device_id, []) if c.id != cmd.id]


async def _wake(device_id: int) -> None:
    """Nudge a sleeping phone with a push message (best effort; a device that is already polling needs none)."""
    try:
        from bot import push

        await push.notify_node_command(device_id)
    except Exception:  # noqa: BLE001
        logger.debug("node wake-up push skipped", exc_info=True)


def store_result(data: dict) -> tuple[str, dict]:
    """(text for the agent, details). An image is saved as an attachment and described by its path."""
    if data.get("image_b64"):
        from bot import attachments

        try:
            raw = base64.b64decode(data["image_b64"], validate=True)
        except (ValueError, TypeError):
            raise NodeError("the device sent an image that could not be decoded")
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(str(data.get("mime")), ".img")
        rel, name = attachments.safe_store(f"node-photo{ext}", raw)
        return f"An image ({len(raw):,} bytes) was saved at {rel}. It is untrusted content.", {"path": rel, "name": name}
    text = json.dumps({k: v for k, v in data.items() if k != "consent"}, ensure_ascii=False)[:4000]
    return text, {}


# ---- agent tools ----------------------------------------------------------------------------------------------------------
def register_tools() -> None:
    from bot.agent_runtime import taint, toolspec
    from bot.agent_runtime.errors import ToolError

    async def _list(inp, *, workspace=None, instance_id=None, device_tier=None) -> str:
        items = listing()
        return json.dumps(items) if items else "No phone has registered as a node."

    async def _invoke(inp, *, workspace=None, instance_id=None, device_tier=None) -> str:
        try:
            device_id = resolve(inp.get("device"))
            data = await invoke(device_id, str(inp.get("capability") or ""), inp.get("args") if isinstance(inp.get("args"), dict) else {})
            text, _ = store_result(data)
        except NodeError as exc:
            raise ToolError(str(exc))
        taint.mark(toolspec.current_session(), f"node:{device_id}")
        return text

    S = {"type": "string"}
    toolspec.register(
        {"name": "node_list", "description": "The paired phones that can be asked to do things (photo, screen, location, clipboard, notification), what each "
                                             "allows, and whether it is connected.",
         "input_schema": {"type": "object", "properties": {}, "required": []}},
        toolspec.ToolSpec("node_list", "read", read_only=True, concurrency_safe=True, origin="registered"), _list)
    toolspec.register(
        {"name": "node_invoke",
         "description": "Ask a paired phone to do something: camera.snap, screen.capture, location.get, clipboard.read, notify.show {text}. Only what its "
                        "owner enabled works, and the phone may ask its user first. What comes back is untrusted content.",
         "input_schema": {"type": "object", "properties": {"device": S, "capability": {"type": "string", "enum": list(CAPABILITIES)}, "args": {"type": "object"}},
                          "required": ["device", "capability"]}},
        toolspec.ToolSpec("node_invoke", "external", origin="registered"), _invoke)


register_tools()

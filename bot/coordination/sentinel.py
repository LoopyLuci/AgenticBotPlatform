"""SentinelBoard: AgentSentinel's work board (/api/v1/board/*) behind the Board interface.

AgentSentinel is the machine-wide watch tower; when it runs, every agent on the machine (ABP's swarms and sub-agents,
the dev swarm, a conductor's workers) shares its one board, so locks and tasks are honoured across processes. This
client is plain blocking HTTP (callers run it in a thread: `blocking = True`). Errors come back as BoardError with the
protocol's code (TAKEN, LOCKED, DEPS, VERSION, STALE_FENCE, NOT_OWNER, AGENT_MISMATCH, ...) and, for LOCKED, the
holders; a connection that cannot be made raises Unavailable.

Where it lives: `coordination.sentinel_url` in config/backends.yaml (default http://127.0.0.1:8080), or the
AGENT_SENTINEL_URL environment variable; the token is read from AGENT_SENTINEL_TOKEN (never from the config file).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

import httpx

from bot.coordination.base import Board, vault_note as _vault_note
from bot.coordination.model import BoardError, Unavailable

DEFAULT_URL = "http://127.0.0.1:8080"
API = "/api/v1/board"


def sentinel_url() -> str:
    env = os.environ.get("AGENT_SENTINEL_URL", "").strip()
    if env:
        return env.rstrip("/")
    try:
        from bot.config import config

        cfg = (config.current.get("coordination") or {}).get("sentinel_url")
        if cfg:
            return str(cfg).rstrip("/")
    except Exception:  # noqa: BLE001 - config trouble must not decide where the board is
        pass
    port = _machine_config().get("http_port")
    return f"http://127.0.0.1:{int(port)}" if isinstance(port, int) else DEFAULT_URL


def sentinel_token() -> str:
    """AGENT_SENTINEL_TOKEN, else the agent token of this machine's AgentSentinel (its config file, as the
    `agent-sentinel` CLI finds it) — so a local AgentSentinel is used without copying a token anywhere."""
    return os.environ.get("AGENT_SENTINEL_TOKEN", "").strip() or str(_machine_config().get("agent_token") or "").strip()


def _machine_config() -> dict:
    """This machine's AgentSentinel config.json (AGENT_SENTINEL_HOME, else %APPDATA%/AgentSentinel or
    ~/.config/agent-sentinel), or {} when there is none or it can't be read. Only the port and agent token are used."""
    home = os.environ.get("AGENT_SENTINEL_HOME", "").strip()
    if home:
        folder = Path(home)
    elif os.name == "nt":
        folder = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "AgentSentinel"
    else:
        folder = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "agent-sentinel"
    try:
        data = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _alias(item: Any) -> Any:
    """The daemon names a lock's task title `task_title` (and, in a LOCKED holder, the requested resource `resource`
    and the held one `held_resource`); ABP's callers read `title`, and `resource` as the held one."""
    if isinstance(item, dict):
        if "task_title" in item and "title" not in item:
            item["title"] = item["task_title"]
        if "held_resource" in item:
            item.setdefault("requested", item["resource"])
            item["resource"] = item["held_resource"]
    return item


def _error(resp: httpx.Response) -> BoardError:
    code, message, holders = "", "", []
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if isinstance(body, dict):
        # The daemon's shape: {version, error: "Request Error", code: "TAKEN", detail: "...", ...extra fields, holders?}.
        # Older/other shapes ({error: CODE, message}, {detail: {code, message, holders}}) are read too.
        node: Any = body.get("detail", body)
        if isinstance(node, dict) and node is not body:
            code = str(node.get("code") or "")
            message = str(node.get("message") or node.get("detail") or "")
            holders = node.get("holders") or body.get("holders") or []
        else:
            if isinstance(body.get("code"), str):
                code = body["code"]
            elif isinstance(body.get("error"), str) and body["error"].isupper():
                code = body["error"]
            message = str(node) if isinstance(node, str) else str(body.get("message") or "")
            holders = body.get("holders") or []
    if not code:
        code = {400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT"}.get(
            resp.status_code, f"HTTP_{resp.status_code}")
    return BoardError(code, message or resp.text[:200], status=resp.status_code, holders=[_alias(h) for h in holders])


class SentinelBoard(Board):
    name = "sentinel"
    blocking = True

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None, *, timeout: float = 10.0):
        self.url = (url or sentinel_url()).rstrip("/")
        self.token = sentinel_token() if token is None else token
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self._hint = threading.local()
        self._http = httpx.Client(base_url=self.url + API, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, *, body: Optional[dict] = None, params: Optional[dict] = None) -> httpx.Response:
        try:
            resp = self._http.request(method, path, json=body, params={k: v for k, v in (params or {}).items() if v not in (None, "")})
        except httpx.HTTPError as exc:
            raise Unavailable(f"AgentSentinel at {self.url} unreachable: {exc}") from exc
        if resp.status_code >= 500:
            raise Unavailable(f"AgentSentinel answered {resp.status_code}")
        if resp.status_code >= 400:
            raise _error(resp)
        return resp

    @staticmethod
    def _drop_none(d: dict) -> dict:
        return {k: v for k, v in d.items() if v is not None}

    # ---- tasks ----
    def create_task(self, board, title, *, key=None, detail="", priority=0, needs=None, deps=None, scope=None,
                    max_attempts=3, dedupe_done=False, created_by=""):
        body = self._drop_none({"board": board, "key": key, "title": title, "detail": detail, "priority": priority,
                                "needs": needs or [], "deps": deps or [], "scope": scope or {"resources": []},
                                "max_attempts": max_attempts, "dedupe_done": dedupe_done or None,
                                "agent": created_by or None})        # the daemon records the creator from `agent`
        resp = self._call("POST", "/tasks", body=body)
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("task"), dict):
            if not data.get("deduped"):
                _vault_note("created", data["task"])
            return data["task"], bool(data.get("deduped", resp.status_code == 200))
        return data, False

    def get_task(self, task_id):
        return self._call("GET", f"/tasks/{task_id}").json()

    def list_tasks(self, board=None, *, state=None, owner=None, limit=200):
        return self._call("GET", "/tasks", params={"board": board, "state": state, "owner": owner, "limit": limit}).json()["tasks"]

    def next(self, board, agent, *, capabilities=None, lease_s=None):
        resp = self._call("POST", "/tasks/next", body=self._drop_none(
            {"board": board, "agent": agent, "capabilities": list(capabilities or []), "lease_s": lease_s}))
        if resp.status_code == 204:
            try:
                self._hint.value = json.loads(resp.headers.get("X-Board-Hint") or "{}")
            except ValueError:
                self._hint.value = {}
            return None
        return resp.json()

    @property
    def last_hint(self) -> dict:
        return getattr(self._hint, "value", {})

    def claim(self, task_id, agent, *, lease_s=None, expect_version=None):
        return self._call("POST", f"/tasks/{task_id}/claim", body=self._drop_none(
            {"agent": agent, "lease_s": lease_s, "expect_version": expect_version})).json()

    def heartbeat(self, task_id, agent, fence, *, lease_s=None, progress=None):
        return self._call("POST", f"/tasks/{task_id}/heartbeat", body=self._drop_none(
            {"agent": agent, "fence": fence, "lease_s": lease_s, "progress": progress})).json()

    def set_state(self, task_id, agent, fence, state, *, result=None, note=""):
        if state == "blocked":
            # AgentSentinel's board has no blocked state (running | review | done | failed | released). `review` is leased and would
            # be requeued, so a blocked task there ends `failed` with a BLOCKED: note and its context as the result; the conductor's
            # own list (bot/conductor/store.py) is the durable record and unblocking re-plans the task.
            state, note = "failed", note if note.startswith("BLOCKED") else f"BLOCKED: {note}"
            result = {**(result if isinstance(result, dict) else {}), "blocked": True}
        out = self._call("POST", f"/tasks/{task_id}/state", body=self._drop_none(
            {"agent": agent, "fence": fence, "state": state, "result": result, "note": note or None})).json()
        if state in ("review", "done", "failed") and isinstance(out, dict):
            _vault_note(state, {**out, "result": out.get("result", result), "note": out.get("note", note)})
        return out

    def cancel(self, task_id, *, agent="", note=""):
        return self._call("POST", f"/tasks/{task_id}/cancel", body=self._drop_none(
            {"agent": agent or None, "note": note or None})).json()

    # ---- locks ----
    def acquire(self, agent, resources, *, task=None, lease_s=None):
        items = [r if isinstance(r, dict) else {"resource": r} if isinstance(r, str) else
                 {"resource": r[0], "mode": r[1]} for r in resources]
        return self._call("POST", "/locks/acquire", body=self._drop_none(
            {"agent": agent, "resources": items, "task": task, "lease_s": lease_s})).json()

    def lock_heartbeat(self, agent, fence, *, lease_s=None):
        return self._call("POST", "/locks/heartbeat", body=self._drop_none(
            {"agent": agent, "fence": fence, "lease_s": lease_s})).json()

    def release_locks(self, agent, fence, resources=None):
        return self._call("POST", "/locks/release", body=self._drop_none(
            {"agent": agent, "fence": fence, "resources": resources})).json()

    def list_locks(self, *, resource_prefix="", owner=""):
        return [_alias(lk) for lk in self._call("GET", "/locks", params={"resource_prefix": resource_prefix, "owner": owner}).json()["locks"]]

    # ---- presence, visibility ----
    def presence(self, agent, status, *, task=None, capabilities=None):
        return self._call("POST", "/presence", body=self._drop_none(
            {"agent": agent, "status": status, "task": task, "capabilities": capabilities})).json()

    def list_presence(self):
        return self._call("GET", "/presence").json()["agents"]

    def summary(self, board=None):
        data = self._call("GET", "/summary", params={"board": board}).json()
        if isinstance(data, dict):
            data.setdefault("backend", self.name)
            for b in (data.get("boards") or {}).values():
                for lk in b.get("holders") or []:
                    _alias(lk)
                for t in b.get("blocked") or []:
                    for h in t.get("holders") or []:
                        _alias(h)
        return data

    def ping(self) -> bool:
        try:
            self._call("GET", "/presence")
            return True
        except BoardError:
            return False

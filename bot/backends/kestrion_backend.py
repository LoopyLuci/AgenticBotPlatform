"""The "kestrion" backend: a bot whose turns are answered by Kestrion's agent (Kestrion's ADR-0097).

Kestrion's remote session API relays a message to the agent session that is open on its desktop and answers the
POST only when that whole turn is done; the reply itself is published on the session's events socket (and kept in
its message list), not in the POST's answer. So one ask is:

  1. subscribe to  GET  /api/v1/sessions/{agent_type}/events   (WebSocket, chat_message events)
  2.               POST /api/v1/sessions/{agent_type}/messages {"content": prompt}
  3. the reply: the last assistant message seen since sending that is no longer streaming; if the socket missed it,
     the same from GET .../messages.

The agent type is the bot's model field (like custom_model's provider/model), else backends.kestrion.
default_agent_type. Kestrion only accepts a message for the agent type that is active on its screen (409 otherwise):
that is its own rule, reported here as it is.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import httpx

from bot.backends.base import Backend, BackendError, BackendResult

logger = logging.getLogger("bot.backends.kestrion")


def _final_assistant(records: list[dict]) -> Optional[dict]:
    """The last assistant message that has finished (streaming is false or absent) and says something."""
    for m in reversed(records):
        if m.get("role") == "assistant" and not m.get("streaming") and str(m.get("content") or "").strip():
            return m
    return None


class KestrionBackend(Backend):
    name = "kestrion"

    def __init__(self, base_url: str, device_token: str, agent_type: str = "build") -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.device_token = device_token or ""
        self.agent_type = agent_type or "build"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.device_token}"}

    async def _listen(self, seen: list[dict], ready: asyncio.Event) -> None:
        """Collect this session's chat_message events until cancelled. A socket that cannot connect is not fatal:
        the message list is read afterwards."""
        import websockets

        url = ("wss" if self.base_url.startswith("https") else "ws") + self.base_url[self.base_url.index("://"):] + \
            f"/api/v1/sessions/{self.agent_type}/events"
        try:
            async with websockets.connect(url, additional_headers=self._headers(), open_timeout=10,
                                          max_size=8 << 20) as ws:
                ready.set()
                async for raw in ws:
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        continue
                    if ev.get("type") == "chat_message" and isinstance(ev.get("data"), dict):
                        seen.append(ev["data"])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.debug("kestrion events socket: %s", e)
        finally:
            ready.set()

    async def ask(self, prompt: str, *, context: Optional[dict] = None, timeout_s: float = 120) -> BackendResult:
        if not self.base_url or not self.device_token:
            raise BackendError("Kestrion isn't linked: in Kestrion, open Settings -> ABP Connector and let ABP use "
                               "Kestrion as a backend")
        timeout_s = max(float(timeout_s or 0), 130.0)          # Kestrion itself waits up to 120 s for a turn
        seen: list[dict] = []
        ready = asyncio.Event()
        listener = asyncio.create_task(self._listen(seen, ready))
        try:
            await asyncio.wait_for(ready.wait(), timeout=12)
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                try:
                    before = await client.get(f"{self.base_url}/api/v1/sessions/{self.agent_type}/messages",
                                              headers=self._headers(), timeout=20)
                    known = {m.get("id") for m in (before.json() if before.status_code == 200 else [])
                             if isinstance(m, dict)}
                    r = await client.post(f"{self.base_url}/api/v1/sessions/{self.agent_type}/messages",
                                          json={"content": prompt}, headers=self._headers())
                except httpx.TimeoutException as e:
                    raise BackendError(f"Kestrion did not finish the turn within {timeout_s:.0f}s") from e
                except httpx.HTTPError as e:
                    raise BackendError(f"Kestrion does not answer at {self.base_url} (is it open?): {e}") from e
                body: Any
                try:
                    body = r.json()
                except ValueError:
                    body = {}
                detail = str((body or {}).get("error") or "") if isinstance(body, dict) else ""
                if r.status_code == 409:
                    raise BackendError(f"Kestrion is not on the \"{self.agent_type}\" agent right now, and it only "
                                       f"takes messages for the one on its screen: {detail}")
                if r.status_code == 401:
                    raise BackendError("Kestrion refused ABP's device token (revoked?): grant access again from "
                                       "Kestrion's ABP Connector")
                if r.status_code == 504:
                    raise BackendError(f"Kestrion's own 120 s wait for the turn ran out: {detail}")
                if r.status_code >= 400:
                    raise BackendError(f"Kestrion answered HTTP {r.status_code}: {detail or r.text[:300]}")
                # the turn is done: its reply is the last finished assistant message we had not seen before
                for _ in range(20):
                    reply = _final_assistant([m for m in seen if m.get("id") not in known])
                    if reply is None:
                        lst = await client.get(f"{self.base_url}/api/v1/sessions/{self.agent_type}/messages",
                                               headers=self._headers(), timeout=20)
                        if lst.status_code == 200 and isinstance(lst.json(), list):
                            reply = _final_assistant([m for m in lst.json() if isinstance(m, dict)
                                                      and m.get("id") not in known])
                    if reply is not None:
                        return BackendResult(text=str(reply["content"]), tokens=reply.get("token_count"),
                                             raw={"message_id": reply.get("id"), "model": reply.get("model_id"),
                                                  "agent_type": self.agent_type})
                    await asyncio.sleep(0.5)
                raise BackendError("Kestrion finished the turn but published no reply for it")
        finally:
            listener.cancel()
            try:
                await listener
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

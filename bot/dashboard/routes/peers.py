"""Dashboard routes: peers.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

import os
import socket

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import db


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _peer_public, _require_token, _require_token_or_api_key

    # Linking this AgenticBotPlatform installation to another one (see bot/peers.py)
    # so an admin running several boxes (a home PC, a laptop, a VPS) can
    # see and manage every one of them from any single dashboard.
    #
    # Two different tokens for two different jobs (see bot/peers.py's
    # module docstring for the full rationale): generating a pairing token
    # and link/unlink all require the strict local DASHBOARD_TOKEN, same
    # bar as every other trust-establishing action in this file — but
    # /api/peers/handshake deliberately does NOT, since its entire job is
    # to be the one endpoint a *different* server's admin calls into. Its
    # auth is the short-lived, single-use pairing token in the payload
    # itself, checked first thing inside peers.accept_handshake(). Reading
    # the list and proxying a peer's own overview/bots/actions stay on
    # _require_token_or_api_key, matching every other bot-management route
    # a paired device can already reach.

    @app.get("/api/peers/self-address", dependencies=[Depends(_require_token)])
    async def api_peers_self_address():
        from bot import peers

        return {"base_url": peers.detect_own_base_url()}

    @app.get("/api/peers/firewall-status", dependencies=[Depends(_require_token)])
    async def api_peers_firewall_status():
        from bot import firewall

        port = int(os.environ.get("DASHBOARD_PORT", "8787"))
        return firewall.status(port)

    @app.post("/api/peers/firewall-open", dependencies=[Depends(_require_token)])
    def api_peers_firewall_open():
        from bot import firewall

        port = int(os.environ.get("DASHBOARD_PORT", "8787"))
        ok, message = firewall.open_inbound_port(port)
        if ok:
            db.log_audit(actor="dashboard", action="firewall_rule_added", detail=message)
        return {"ok": ok, "message": message}

    @app.post("/api/peers/pairing-token", dependencies=[Depends(_require_token)])
    def api_peers_pairing_token(payload: dict = Body(default={})):
        from bot import peers

        base_url = (payload.get("base_url") or "").strip() or None
        try:
            result = peers.generate_pairing_token(base_url)
        except peers.PeerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="peer_pairing_token_generated", detail="generated a server pairing token")
        return result

    @app.post("/api/peers/link", dependencies=[Depends(_require_token)])
    async def api_peers_link(payload: dict = Body(...)):
        from bot import peers

        name = (payload.get("name") or "").strip()
        pairing_token = payload.get("pairing_token") or ""
        my_base_url = (payload.get("my_base_url") or "").strip() or None
        setup_ssh = payload.get("setup_ssh", True)
        if not name or not pairing_token:
            raise HTTPException(status_code=400, detail="payload must be {name, pairing_token, my_base_url?, setup_ssh?}")
        my_name = os.environ.get("AGENTICBOTPLATFORM_NAME") or socket.gethostname()
        try:
            peer = await peers.link_peer(name, pairing_token, my_name, my_base_url, setup_ssh=bool(setup_ssh))
        except peers.PeerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="peer_link", detail=f"linked peer {peer['id']} ({peer['name']!r})")
        return {"ok": True, "peer": _peer_public(peer)}

    @app.post("/api/peers/handshake")
    async def api_peers_handshake(payload: dict = Body(...)):
        from bot import peers

        name = (payload.get("name") or "").strip()
        api_key = payload.get("api_key") or ""
        base_url = payload.get("base_url")
        pairing_token = payload.get("pairing_token") or ""
        my_name = os.environ.get("AGENTICBOTPLATFORM_NAME") or socket.gethostname()
        try:
            result = await peers.accept_handshake(
                name, api_key, base_url, my_name, pairing_token,
                ssh_public_key=payload.get("ssh_public_key"), ssh_username=payload.get("ssh_username"),
            )
        except peers.PeerError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="peer_handshake", detail=f"accepted handshake from {name!r}")
        return result

    @app.get("/api/peers", dependencies=[Depends(_require_token_or_api_key)])
    def api_peers_list():
        return [_peer_public(dict(r)) for r in db.list_peer_servers()]

    @app.delete("/api/peers/{peer_id}", dependencies=[Depends(_require_token)])
    def api_peers_unlink(peer_id: int):
        from bot import peers

        row = peers.unlink_peer(peer_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such linked server")
        db.log_audit(actor="dashboard", action="peer_unlink", detail=f"unlinked peer {peer_id} ({row['name']!r})")
        return {"ok": True}

    @app.get("/api/peers/{peer_id}/overview", dependencies=[Depends(_require_token_or_api_key)])
    async def api_peers_overview(peer_id: int):
        from bot import peers

        row = db.get_peer_server(peer_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such linked server")
        try:
            return await peers.fetch_overview(row)
        except peers.PeerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/peers/{peer_id}/bots", dependencies=[Depends(_require_token_or_api_key)])
    async def api_peers_bots(peer_id: int):
        from bot import peers

        row = db.get_peer_server(peer_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such linked server")
        try:
            return await peers.fetch_bots(row)
        except peers.PeerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/peers/{peer_id}/bots/{instance_id}/{action}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_peers_bot_action(peer_id: int, instance_id: int, action: str):
        from bot import peers

        row = db.get_peer_server(peer_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such linked server")
        try:
            result = await peers.run_bot_action(row, instance_id, action)
        except peers.PeerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="peer_bot_action", detail=f"{action} on peer {peer_id}'s bot {instance_id}")
        return result

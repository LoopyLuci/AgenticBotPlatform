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
    #
    # Nothing in the default flow asks anyone to open a port: the pairing
    # token carries this machine's TransferDaemon overlay invite, the
    # handshake rides the overlay endpoint, and /api/peers/firewall-open
    # stays what it has always been — an explicit, manual, advanced action
    # for a direct address on a private network, which ABP never offers on
    # its own.

    @app.get("/api/peers/self-address", dependencies=[Depends(_require_token)])
    async def api_peers_self_address():
        from bot import peers

        return {"base_url": peers.detect_own_base_url()}

    @app.get("/api/peers/transport", dependencies=[Depends(_require_token)])
    async def api_peers_transport():
        """This machine's TransferDaemon: is the overlay up, and should ABP offer to install it.

        The offer matters because without TransferDaemon linking still works — directly — but only between
        machines that can already reach each other's port."""
        from bot import peers

        return peers.td_state()

    @app.get("/api/peers/routes", dependencies=[Depends(_require_token_or_api_key)])
    async def api_peers_routes():
        """How each linked peer is reached right now: overlay kind, RTT, loss and state, or the direct address.

        The heartbeat refreshes these, so a route that changed shows up here on its own."""
        from bot import peers

        return peers.td_routes()

    @app.get("/api/peers/firewall-status", dependencies=[Depends(_require_token)])
    async def api_peers_firewall_status():
        from bot import firewall

        port = int(os.environ.get("DASHBOARD_PORT", "8787"))
        return firewall.status(port)

    @app.post("/api/peers/firewall-open", dependencies=[Depends(_require_token)])
    def api_peers_firewall_open():
        """Add the named inbound rule for this machine's dashboard port.

        Advanced and manual on purpose, never part of pairing: with TransferDaemon on both machines a link
        needs no open port at all. This only matters for a direct address on a private network."""
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
        name = (payload.get("name") or "").strip() or (os.environ.get("AGENTICBOTPLATFORM_NAME") or socket.gethostname())
        try:
            result = peers.generate_pairing_token(base_url, name)
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
                transferdaemon=payload.get("transferdaemon") or {},
            )
        except peers.PeerError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="peer_handshake", detail=f"accepted handshake from {name!r}")
        return result

    @app.post("/api/peers/{peer_id}/proxy", dependencies=[Depends(_require_token)])
    async def api_peers_proxy(peer_id: str, body: dict = Body(...)):
        """Call a linked server's VM-Harness, Hermes Manager or power API (whatever it allows) from this dashboard.
        Body: {method, path, body}. peer_id may be the peer's id or name."""
        from bot import peers

        try:
            row = peers.find_peer(peer_id)
            result = await peers.proxy(row, str(body.get("method") or "GET"), str(body.get("path") or ""), body.get("body"))
        except peers.PeerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if str(body.get("method") or "GET").upper() != "GET":
            db.log_audit(actor="dashboard", action="peer_proxy", detail=f"{row['name']}: {body.get('method')} {body.get('path')}")
        return {"result": result}

    @app.get("/api/peers/control", dependencies=[Depends(_require_token_or_api_key)])
    async def api_peers_control():
        """What this machine lets linked servers control (peers.remote_control), and every area there is."""
        from bot import peers

        return {"allowed": sorted(peers.allowed_control_areas()), "areas": sorted(peers.CONTROL_AREAS)}

    @app.put("/api/peers/control", dependencies=[Depends(_require_token)])
    async def api_peers_control_set(body: dict = Body(...)):
        """Choose what linked servers may control on this machine: {allowed: [vm-harness, hermes-manager, power]}.
        Only this machine's own dashboard can change it; a linked server never can."""
        from bot import peers
        from bot.config import config

        allowed = body.get("allowed")
        if not isinstance(allowed, list) or any(str(a) not in peers.CONTROL_AREAS for a in allowed):
            raise HTTPException(status_code=400, detail="allowed is a list of: " + ", ".join(sorted(peers.CONTROL_AREAS)))
        config.set_values({("peers", "remote_control"): sorted({str(a) for a in allowed})}, actor="dashboard")
        db.log_audit(actor="dashboard", action="peer_control", detail=f"linked servers may control: {sorted(allowed)}")
        return {"allowed": sorted(peers.allowed_control_areas()), "areas": sorted(peers.CONTROL_AREAS)}

    @app.get("/api/peers", dependencies=[Depends(_require_token_or_api_key)])
    def api_peers_list():
        from bot import peers

        # Each row carries its route (kind, RTT, state) and the address the next call will actually use,
        # so the Linked Servers page can show how a peer is reached without a second request.
        out = []
        for r in db.list_peer_servers():
            row = dict(r)
            row["route"] = peers.route_info(row)
            row["reachable_at"] = peers.peer_base_url(row)
            out.append(_peer_public(row))
        return out

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

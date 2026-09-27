"""Dashboard routes: mobile keys.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
from typing import Optional

import qrcode
from fastapi import Body, Depends, FastAPI, HTTPException
from qrcode.image.pure import PyPNGImage

from bot import db


def register(app: FastAPI) -> None:
    from bot.dashboard.server import (
        _annotate_online,
        _caller_device_id,
        _identify_caller,
        _manager,
        _require_token,
        _require_token_or_api_key,
    )

    # Per-device credentials the Android app pairs with. Creating a key
    # accepts either the desktop token or an existing mobile key — an
    # already-paired phone can mint a sibling key to onboard a new device
    # without the PC. Listing and revoking stay strictly desktop-only:
    # a phone can bring a new device online but can't see or kill other
    # devices' access, so revoking a lost/stolen phone from the desktop
    # still cuts off anything it minted too.

    @app.get("/api/network-info", dependencies=[Depends(_require_token_or_api_key)])
    async def api_network_info():
        from bot import network_info

        loop = asyncio.get_running_loop()
        addrs, funnel_url = await asyncio.gather(
            loop.run_in_executor(None, network_info.detect_addresses),
            loop.run_in_executor(None, network_info.detect_funnel_url),
        )
        port = int(os.environ.get("DASHBOARD_PORT", "8787"))
        return {
            "lan": f"{addrs['lan']}:{port}" if addrs.get("lan") else None,
            "tailscale": f"{addrs['tailscale']}:{port}" if addrs.get("tailscale") else None,
            "funnel": funnel_url,
        }

    @app.post("/api/mobile-keys", dependencies=[Depends(_require_token_or_api_key)])
    async def api_mobile_keys_create(
        payload: dict = Body(...), caller: str = Depends(_identify_caller),
        caller_device_id: Optional[int] = Depends(_caller_device_id),
    ):
        from bot import device_tiers, mobile_pairing

        label = (payload.get("label") or "").strip() or "Unnamed device"
        requested_tier = (payload.get("tier") or "none").strip()
        if not device_tiers.is_valid_tier(requested_tier):
            raise HTTPException(status_code=400, detail=f"unknown permission tier {requested_tier!r}")
        # The desktop DASHBOARD_TOKEN (caller_device_id is None) is the
        # unconditional top authority and may mint at any tier; a device
        # minting a new peer may only grant up to its own tier — see
        # bot/device_tiers.py's can_mint().
        if caller_device_id is not None:
            actor_row = db.get_api_key(caller_device_id)
            actor_tier = actor_row["permission_tier"] if actor_row else "none"
            if not device_tiers.can_mint(actor_tier, requested_tier):
                raise HTTPException(
                    status_code=403,
                    detail=f"your device's own tier ({actor_tier}) can't mint a device at tier {requested_tier!r}",
                )
        key_id, plaintext = db.create_api_key(label, permission_tier=requested_tier)
        db.create_conversations_for_new_device(key_id)
        # host2/host3 are optional additional, independent paths to the same
        # server (e.g. a Tailscale hostname alongside a LAN IP, alongside a
        # public Tailscale Funnel URL) — the Android app tries all
        # configured hosts in order and fails over automatically if one
        # stops answering. Auto-filled with this machine's own detected
        # addresses whenever the caller leaves any blank, so a key minted
        # with no explicit host still gets every automatically-tried
        # network path by default — see bot/mobile_pairing.py's doc.
        host, host2, host3 = await mobile_pairing.detect_hosts(
            (payload.get("host") or "").strip(),
            (payload.get("host2") or "").strip(),
            (payload.get("host3") or "").strip(),
        )
        # The self-contained pairing code — one string carrying every host
        # plus the key, so pasting it alone (no separate host entry) is
        # enough for the app's manual-entry flow, not only its QR scan.
        pairing_code = mobile_pairing.build_pairing_code(plaintext, host, host2, host3)
        img = qrcode.make(pairing_code, image_factory=PyPNGImage)
        buf = io.BytesIO()
        img.save(buf)
        qr_png_base64 = base64.b64encode(buf.getvalue()).decode("ascii")
        db.log_audit(actor=caller, action="mobile_key_create", detail=f"created key {key_id!r} ({label!r})")
        devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
        await _manager.broadcast({"type": "device_list", "devices": _annotate_online([dict(d) for d in devices])})
        return {
            "id": key_id, "label": label, "key": plaintext, "pairing_code": pairing_code, "qr_png_base64": qr_png_base64,
            # Echoed back (not just embedded in the code) so the dashboard UI
            # can show exactly which two paths this key was minted with,
            # including whichever got auto-filled above.
            "host": host or None, "host2": host2 or None, "host3": host3 or None,
        }

    @app.get("/api/mobile-keys", dependencies=[Depends(_require_token)])
    def api_mobile_keys_list():
        return [dict(r) for r in db.list_api_keys(kind="device")]

    @app.put("/api/mobile-keys/{key_id}", dependencies=[Depends(_require_token)])
    async def api_mobile_keys_update(key_id: int, payload: dict = Body(...)):
        try:
            db.update_api_key_label(key_id, payload.get("label", ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="mobile_key_rename", detail=f"renamed key {key_id}")
        devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
        await _manager.broadcast({"type": "device_list", "devices": _annotate_online([dict(d) for d in devices])})
        return {"ok": True}

    @app.delete("/api/mobile-keys/{key_id}", dependencies=[Depends(_require_token)])
    async def api_mobile_keys_revoke(key_id: int):
        db.revoke_api_key(key_id)
        db.log_audit(actor="dashboard", action="mobile_key_revoke", detail=f"revoked key {key_id}")
        devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
        await _manager.broadcast({"type": "device_list", "devices": _annotate_online([dict(d) for d in devices])})
        return {"ok": True}

    def _resolve_actor_tier(caller_device_id: Optional[int]) -> str:
        # Desktop (caller_device_id is None) is the unconditional top
        # authority for device-tier management — see bot/device_tiers.py.
        if caller_device_id is None:
            return "unrestricted"
        row = db.get_api_key(caller_device_id)
        return row["permission_tier"] if row else "none"

    @app.post("/api/mobile-keys/{key_id}/tier", dependencies=[Depends(_require_token_or_api_key)])
    async def api_mobile_keys_set_tier(
        key_id: int, payload: dict = Body(...), caller: str = Depends(_identify_caller),
        caller_device_id: Optional[int] = Depends(_caller_device_id),
    ):
        from bot import device_tiers

        new_tier = (payload.get("tier") or "").strip()
        if not device_tiers.is_valid_tier(new_tier):
            raise HTTPException(status_code=400, detail=f"unknown permission tier {new_tier!r}")
        target = db.get_api_key(key_id)
        if target is None:
            raise HTTPException(status_code=404, detail="no such device")
        actor_tier = _resolve_actor_tier(caller_device_id)
        is_self = caller_device_id is not None and caller_device_id == key_id
        if caller_device_id is not None:
            if not device_tiers.can_manage(actor_tier, target["permission_tier"], is_self=is_self):
                raise HTTPException(status_code=403, detail="your device can only change the tier of a strictly lower-tier device")
            if not device_tiers.can_mint(actor_tier, new_tier):
                raise HTTPException(status_code=403, detail=f"your device's own tier ({actor_tier}) can't grant tier {new_tier!r}")
        db.set_api_key_tier(key_id, new_tier)
        db.log_audit(actor=caller, action="mobile_key_set_tier", detail=f"set key {key_id} tier -> {new_tier!r}")
        devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
        await _manager.broadcast({"type": "device_list", "devices": _annotate_online([dict(d) for d in devices])})
        return {"ok": True, "permission_tier": new_tier}

    @app.post("/api/mobile-keys/{key_id}/revoke-by-device", dependencies=[Depends(_require_token_or_api_key)])
    async def api_mobile_keys_revoke_by_device(
        key_id: int, caller: str = Depends(_identify_caller),
        caller_device_id: Optional[int] = Depends(_caller_device_id),
    ):
        """Device-callable revoke, distinct from the desktop-only DELETE
        route above — a device may only revoke a strictly lower-tier
        device, never itself, never a peer/superior. The desktop route
        stays the unconditional, ungated path."""
        from bot import device_tiers

        target = db.get_api_key(key_id)
        if target is None:
            raise HTTPException(status_code=404, detail="no such device")
        if caller_device_id is not None:
            actor_tier = _resolve_actor_tier(caller_device_id)
            is_self = caller_device_id == key_id
            if not device_tiers.can_manage(actor_tier, target["permission_tier"], is_self=is_self):
                raise HTTPException(status_code=403, detail="your device can only revoke a strictly lower-tier device")
        db.revoke_api_key(key_id)
        db.log_audit(actor=caller, action="mobile_key_revoke", detail=f"revoked key {key_id}")
        devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
        await _manager.broadcast({"type": "device_list", "devices": _annotate_online([dict(d) for d in devices])})
        return {"ok": True}

    @app.post("/api/mobile-keys/purge-revoked", dependencies=[Depends(_require_token)])
    def api_mobile_keys_purge_revoked():
        n = db.purge_revoked_keys()
        db.log_audit(actor="dashboard", action="mobile_keys_purge_revoked", detail=f"removed {n} revoked key(s)")
        return {"ok": True, "purged": n}

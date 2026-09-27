"""Dashboard routes: android apk.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from bot import db, push
from bot import tasks as bg


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _caller_device_id, _require_mobile_key_id, _require_token, _require_token_or_api_key

    # Sends the last APK built on this server to one or every paired device.
    # Callable from the desktop dashboard OR from any already-paired phone
    # (Devices screen's own Send / Send to all devices buttons) — either way
    # it's the same server-mediated queue, not a direct device-to-device
    # transfer. Pull-based, deliberately: there's no reliable way to wake a
    # backgrounded phone without FCM (optional, often unconfigured), so
    # "send" just queues an apk_pushes row and the phone picks it up on its
    # own next /api/android/apk/pending poll — see bot/db.py's apk_pushes
    # table comment.

    @app.get("/api/android/apk/status", dependencies=[Depends(_require_token)])
    def api_android_apk_status():
        from bot.android_apk import latest_apk_path

        path = latest_apk_path()
        if not path.is_file():
            return {"available": False}
        stat = path.stat()
        return {
            "available": True,
            "built_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "size_bytes": stat.st_size,
        }

    @app.post("/api/android/apk/send")
    async def api_android_apk_send(payload: dict = Body(...), caller_device_id: Optional[int] = Depends(_caller_device_id)):
        api_key_id = payload.get("api_key_id")
        if not isinstance(api_key_id, int):
            raise HTTPException(status_code=400, detail="payload must be {api_key_id: <int>}")
        mesh = bool(payload.get("mesh"))
        if mesh:
            # The caller itself is the source — its own installed APK,
            # served directly to the target over the LAN by its own mesh
            # listener. This server never touches the bytes; it only mints
            # the one-time token the target will present to that listener.
            if caller_device_id is None:
                raise HTTPException(status_code=400, detail="mesh sends must come from a paired device, not the desktop dashboard")
            token = secrets.token_urlsafe(24)
            push_id = db.create_apk_push(
                api_key_id, "", version_label="mesh", origin_api_key_id=caller_device_id, mesh_token=token,
            )
            db.log_audit(actor="dashboard", action="apk_send_mesh", detail=f"queued mesh apk push {push_id} from device {caller_device_id} to {api_key_id}")
            bg.spawn(push.notify_apk_push(api_key_id, push_id, "mesh"))
            return {"ok": True, "push_id": push_id}
        from bot.android_apk import apk_version_label, latest_apk_path

        path = latest_apk_path()
        if not path.is_file():
            raise HTTPException(status_code=400, detail="no built APK found — build one first")
        version_label = apk_version_label(path)
        push_id = db.create_apk_push(api_key_id, str(path), version_label=version_label)
        db.log_audit(actor="dashboard", action="apk_send", detail=f"queued apk push {push_id} for device {api_key_id}")
        bg.spawn(push.notify_apk_push(api_key_id, push_id, version_label))
        return {"ok": True, "push_id": push_id}

    @app.post("/api/android/apk/send-all")
    async def api_android_apk_send_all(payload: Optional[dict] = Body(default=None), caller_device_id: Optional[int] = Depends(_caller_device_id)):
        mesh = bool((payload or {}).get("mesh"))
        keys = [r for r in db.list_api_keys(kind="device") if not r["revoked_at"]]
        if mesh:
            if caller_device_id is None:
                raise HTTPException(status_code=400, detail="mesh sends must come from a paired device, not the desktop dashboard")
            push_ids = []
            for r in keys:
                if r["id"] == caller_device_id:
                    continue  # don't queue a push to yourself
                token = secrets.token_urlsafe(24)
                pid = db.create_apk_push(
                    r["id"], "", version_label="mesh", origin_api_key_id=caller_device_id, mesh_token=token,
                )
                push_ids.append(pid)
                bg.spawn(push.notify_apk_push(r["id"], pid, "mesh"))
            db.log_audit(actor="dashboard", action="apk_send_all_mesh", detail=f"queued mesh apk push from device {caller_device_id} for {len(push_ids)} device(s)")
            return {"ok": True, "sent_to": len(push_ids)}
        from bot.android_apk import apk_version_label, latest_apk_path

        path = latest_apk_path()
        if not path.is_file():
            raise HTTPException(status_code=400, detail="no built APK found — build one first")
        version_label = apk_version_label(path)
        push_ids = []
        for r in keys:
            pid = db.create_apk_push(r["id"], str(path), version_label=version_label)
            push_ids.append(pid)
            bg.spawn(push.notify_apk_push(r["id"], pid, version_label))
        db.log_audit(actor="dashboard", action="apk_send_all", detail=f"queued apk push for {len(push_ids)} device(s)")
        return {"ok": True, "sent_to": len(push_ids)}

    @app.post("/api/android/apk/mesh/redeem", dependencies=[Depends(_require_token_or_api_key)])
    def api_android_apk_mesh_redeem(payload: dict = Body(...), caller_device_id: Optional[int] = Depends(_caller_device_id)):
        """Called by the *origin* device's own mesh listener (not the
        target) right after it accepts an incoming socket connection and
        reads the token the target presented — this confirms with the
        server that the token is real, matches this exact push, was minted
        for this device to hand out, and hasn't already been spent, before
        the origin streams a single byte of its APK to whoever's asking."""
        push_id = payload.get("push_id")
        token = payload.get("token")
        if not isinstance(push_id, int) or not isinstance(token, str):
            raise HTTPException(status_code=400, detail="payload must be {push_id: <int>, token: <str>}")
        row = db.get_apk_push(push_id)
        if row is None or row["origin_api_key_id"] != caller_device_id:
            raise HTTPException(status_code=404, detail="no such mesh push originating from this device")
        return {"ok": db.redeem_mesh_token(push_id, token)}

    @app.get("/api/turn/credentials", dependencies=[Depends(_require_token_or_api_key)])
    async def api_turn_credentials(caller_device_id: Optional[int] = Depends(_caller_device_id)):
        """Short-lived TURN relay credentials for the WebRTC mesh fallback
        (see bot/turn.py) — minted fresh per call, never stored, so there's
        nothing here to revoke beyond letting the ttl expire. Returns
        {"enabled": false} rather than 404/403 when TURN isn't configured,
        since "no TURN available" is an expected, non-error state the
        client falls back to STUN-only for."""
        from bot import turn

        label = str(caller_device_id) if caller_device_id is not None else "desktop"
        creds = turn.credentials(user_label=label)
        if creds is None:
            return {"enabled": False}
        return {"enabled": True, **creds}

    @app.get("/api/android/apk/pending")
    def api_android_apk_pending(api_key_id: int = Depends(_require_mobile_key_id)):
        row = db.get_pending_apk_push(api_key_id)
        if row is None:
            return {"available": False}
        result = {
            "available": True,
            "push_id": row["id"],
            "version_label": row["version_label"],
            "created_at": row["created_at"],
        }
        origin_id = row["origin_api_key_id"]
        if origin_id:
            presence = db.get_device_presence(origin_id)
            mesh: dict = {"origin_api_key_id": origin_id, "token": row["mesh_token"]}
            if presence and presence["local_ip"] and presence["mesh_port"]:
                # Handed to this device only — it's the sole party the
                # server ever tells about another device's LAN address, and
                # only for the one push actually addressed to it.
                mesh["host"] = presence["local_ip"]
                mesh["port"] = presence["mesh_port"]
            # origin_api_key_id + token are always included even without a
            # usable LAN address — they're what the WebRTC fallback needs to
            # address a signaling offer at the origin device (see
            # WebRtcMeshClient.kt) when the two devices aren't on the same
            # network for the direct-socket path above to work at all.
            result["mesh"] = mesh
        return result

    @app.get("/api/android/apk/download/{push_id}")
    def api_android_apk_download(push_id: int, api_key_id: int = Depends(_require_mobile_key_id)):
        row = db.get_apk_push(push_id)
        if row is None or row["api_key_id"] != api_key_id:
            raise HTTPException(status_code=404, detail="no such pending push for this device")
        if row["origin_api_key_id"]:
            raise HTTPException(status_code=409, detail="this push is mesh-only — no server-side copy exists, retry the direct transfer")
        path = Path(row["apk_path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="APK file no longer available — ask the desktop app to send again")
        db.mark_apk_push_downloaded(push_id)
        return FileResponse(path, media_type="application/vnd.android.package-archive", filename="AgenticBotPlatform.apk")

"""WhatsApp Cloud API platform — inbound webhook + outbound Graph API calls.

Architecturally different from Discord/Slack/Matrix/Telegram: WhatsApp
delivers messages via a webhook POST to one URL you register with Meta,
not an outbound-connecting client holding its own persistent socket.
There is nothing to "connect" — a WhatsApp bot instance's
platform_supervisor task (run_instance() below) is a near no-op that
just registers this instance's outbox sender and blocks, keeping the
enabled/running/start/stop bookkeeping consistent with every other
platform's dashboard controls. The real work happens in
verify_challenge()/handle_webhook_payload(), called from
bot/dashboard/server.py's single, install-wide `/webhooks/whatsapp`
route — Meta only supports one webhook URL per App, shared across every
phone number registered to it, which is why lookup here is keyed by the
`phone_number_id` each inbound payload carries, not by a per-instance URL
the way every other platform's credentials work.

Setup (also walked through in the dashboard's Bots tab):
  1. developers.facebook.com -> create/select an App -> add the "WhatsApp"
     product -> note its Phone Number ID (Cloud API sidebar) and generate
     a permanent access token (System User with the
     whatsapp_business_messaging permission — not the 24-hour test token,
     which expires).
  2. App Dashboard -> WhatsApp -> Configuration -> Webhook: set the
     callback URL to this server's public HTTPS
     `<your-domain>/webhooks/whatsapp`, and the Verify Token to whatever
     you also paste into the instance's "Verify token" field below.
     Subscribe to the "messages" field.
  3. App Dashboard -> Settings -> Basic -> App Secret -> paste into the
     instance's "App secret" field — this is what lets
     bot/dashboard/server.py's webhook route tell a real call from Meta
     apart from anyone else who finds the URL, since this one endpoint
     is necessarily unauthenticated (Meta can't send a dashboard token).
  4. This server must be reachable over HTTPS from Meta's servers — a
     real domain/reverse proxy, or a tunnel (e.g. ngrok) for local
     testing; Meta will not call a bare localhost/http URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import mimetypes
from pathlib import Path
from typing import Any, Optional

import httpx

from bot import attachments, bot_instances, db, platform_supervisor, push
from bot.backends.base import BackendError
from bot.commands import CmdContext, dispatch_command
from bot.router import router

logger = logging.getLogger("bot.platforms.whatsapp")

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"
WHATSAPP_MAX_LEN = 4096

# Per-(instance, sender) scratch state (project_cwd, action_type) for
# /project — WhatsApp has no persistent connection object of its own to
# hang a per-chat dict off of, unlike Discord/Matrix's per-instance class.
_sessions: dict[tuple[int, str], dict] = {}


def verify_challenge(mode: str, token: str, challenge: str) -> Optional[str]:
    """The GET handshake Meta performs once when you save the webhook
    config. Accepts if `token` matches any configured WhatsApp instance's
    verify_token — there's no phone number in this request to narrow it
    to one instance."""
    if mode != "subscribe" or not token:
        return None
    for row in bot_instances.list_instances(platform="whatsapp"):
        if row["credentials"].get("verify_token") == token:
            return challenge
    return None


def verify_signature(raw_body: bytes, header_sig: str) -> bool:
    """Real webhook auth for the POST path — /webhooks/whatsapp itself
    can't require a dashboard token (Meta has no way to send one), so
    this HMAC check against a configured app_secret is the actual
    security boundary. All phone numbers under one Meta App share that
    App's one secret, so this checks against every configured instance's
    app_secret rather than needing to identify the instance first."""
    if not header_sig.startswith("sha256="):
        return False
    provided = header_sig[len("sha256=") :]
    secrets = {row["credentials"].get("app_secret", "") for row in bot_instances.list_instances(platform="whatsapp")}
    for secret in secrets:
        if not secret:
            continue
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, provided):
            return True
    return False


def _find_instance(phone_number_id: str) -> Optional[dict[str, Any]]:
    for row in bot_instances.list_instances(platform="whatsapp", enabled_only=True):
        if row["credentials"].get("phone_number_id") == phone_number_id and platform_supervisor.is_running(row["id"]):
            return row
    return None


async def _send_text(instance: dict[str, Any], to: str, text: str) -> None:
    access_token = instance["credentials"]["access_token"]
    phone_number_id = instance["credentials"]["phone_number_id"]
    text = text or "(empty response)"
    db.log_message(
        platform="whatsapp", chat_id=to, direction="out", source="bot", text=text, instance_id=instance["id"]
    )
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(text), WHATSAPP_MAX_LEN):
            resp = await client.post(
                f"{GRAPH_API_BASE}/{phone_number_id}/messages",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "messaging_product": "whatsapp", "to": to, "type": "text",
                    "text": {"body": text[i : i + WHATSAPP_MAX_LEN]},
                },
            )
            if resp.status_code >= 300:
                logger.warning(
                    "whatsapp send failed for instance %r: %s %s", instance["name"], resp.status_code, resp.text
                )


async def _send_media(instance: dict[str, Any], to: str, file_path: str, filename: str, caption: Optional[str]) -> None:
    access_token = instance["credentials"]["access_token"]
    phone_number_id = instance["credentials"]["phone_number_id"]
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    data = Path(file_path).read_bytes()
    async with httpx.AsyncClient(timeout=60) as client:
        upload_resp = await client.post(
            f"{GRAPH_API_BASE}/{phone_number_id}/media",
            headers={"Authorization": f"Bearer {access_token}"},
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (filename, data, mime)},
        )
        if upload_resp.status_code >= 300:
            raise BackendError(f"whatsapp media upload failed: {upload_resp.status_code} {upload_resp.text}")
        media_id = upload_resp.json()["id"]
        msgtype = (
            "image" if mime.startswith("image/")
            else "video" if mime.startswith("video/")
            else "audio" if mime.startswith("audio/")
            else "document"
        )
        content: dict[str, Any] = {"id": media_id}
        if caption and msgtype != "audio":  # WhatsApp's audio message type has no caption field
            content["caption"] = caption
        db.log_message(
            platform="whatsapp", chat_id=to, direction="out", source="bot",
            text=caption or f"[{msgtype}] {filename}", instance_id=instance["id"],
        )
        send_resp = await client.post(
            f"{GRAPH_API_BASE}/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"messaging_product": "whatsapp", "to": to, "type": msgtype, msgtype: content},
        )
        if send_resp.status_code >= 300:
            raise BackendError(f"whatsapp media send failed: {send_resp.status_code} {send_resp.text}")
    if caption and msgtype == "audio":
        await _send_text(instance, to, caption)


async def _download_media(instance: dict[str, Any], media_id: str) -> tuple[bytes, str]:
    access_token = instance["credentials"]["access_token"]
    async with httpx.AsyncClient(timeout=30) as client:
        meta_resp = await client.get(f"{GRAPH_API_BASE}/{media_id}", headers={"Authorization": f"Bearer {access_token}"})
        meta_resp.raise_for_status()
        info = meta_resp.json()
        data_resp = await client.get(info["url"], headers={"Authorization": f"Bearer {access_token}"})
        data_resp.raise_for_status()
        return data_resp.content, info.get("mime_type", "application/octet-stream")


async def _process_message(instance: dict[str, Any], msg: dict[str, Any], contact_name: str) -> None:
    sender = msg.get("from", "")
    allowed = {str(i) for i in instance["allowed_user_ids"]}
    if sender not in allowed:
        logger.warning("rejected whatsapp message from unauthorized user %s on instance %r", sender, instance["name"])
        db.log_audit(
            actor=sender, action="unauthorized_attempt", detail=f"whatsapp:{sender} (instance {instance['id']})"
        )
        return

    msg_type = msg.get("type")
    text = ""
    if msg_type == "text":
        text = msg.get("text", {}).get("body", "")
    elif msg_type in ("image", "document", "audio", "video", "sticker"):
        media_obj = msg.get(msg_type, {})
        media_id = media_obj.get("id")
        if media_id:
            try:
                data, mime = await _download_media(instance, media_id)
            except Exception as exc:
                logger.warning("whatsapp media download failed for instance %r: %s", instance["name"], exc)
                return
            filename = media_obj.get("filename") or f"{msg_type}{mimetypes.guess_extension(mime) or ''}"
            rel_path, orig_name = attachments.safe_store(filename, data)
            db.log_message(
                platform="whatsapp", chat_id=sender, user_id=sender, username=contact_name,
                direction="in", source="whatsapp", text="", instance_id=instance["id"],
                attachment_path=rel_path, attachment_name=orig_name, attachment_mime=mime,
            )
            asyncio.create_task(push.notify_new_message(instance["name"], f"📎 {orig_name}"))
        text = media_obj.get("caption", "") or ""
    else:
        return  # location/contacts/reactions/status updates — not a chat message to act on

    if not text.strip():
        return

    db.log_message(
        platform="whatsapp", chat_id=sender, user_id=sender, username=contact_name,
        direction="in", source="whatsapp", text=text, instance_id=instance["id"],
    )
    asyncio.create_task(push.notify_new_message(instance["name"], text))

    session = _sessions.setdefault((instance["id"], sender), {})
    cmd_ctx = CmdContext(
        instance_id=instance["id"], instance_name=instance["name"],
        user_id=sender, chat_id=sender, actor=sender, session=session,
    )
    cmd_reply = await dispatch_command(text, cmd_ctx)
    if cmd_reply is not None:
        await _send_text(instance, sender, cmd_reply)
        return

    try:
        result = await router.ask(
            text, action_type=session.get("action_type", "quick_question"), user_id=sender,
            context={"cwd": session["project_cwd"]} if session.get("project_cwd") else None,
            instance_id=instance["id"], chat_id=sender,
        )
        await _send_text(instance, sender, result.text)
    except BackendError as exc:
        await _send_text(instance, sender, f"Backend failed: {exc}")


async def handle_webhook_payload(payload: dict[str, Any]) -> None:
    """Processes one already-signature-verified webhook payload. Iterates
    every change in every entry — a single POST can (rarely) batch events
    for more than one of this install's phone numbers."""
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            if not phone_number_id:
                continue
            instance = _find_instance(phone_number_id)
            if instance is None:
                continue
            contacts = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                await _process_message(instance, msg, contacts.get(msg.get("from"), ""))


async def run_instance(row: dict[str, Any]) -> None:
    """platform_supervisor's task for a WhatsApp instance — see this
    module's docstring for why there's nothing to actually connect."""
    from bot import outbox

    async def _send(chat_id: Any, text: str) -> None:
        await _send_text(row, str(chat_id), text)

    async def _send_file(chat_id: Any, file_path: str, filename: str, caption: Optional[str]) -> None:
        await _send_media(row, str(chat_id), file_path, filename, caption)

    outbox.register(row["id"], _send)
    outbox.register_file_sender(row["id"], _send_file)
    try:
        await asyncio.Event().wait()  # block until this task is cancelled
    finally:
        outbox.unregister(row["id"])
        outbox.unregister_file_sender(row["id"])

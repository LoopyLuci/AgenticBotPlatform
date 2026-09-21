"""E-mail as a channel (roadmap P7): mail an address the bot watches, get the answer back as a reply.

    credentials: imap_host, imap_port, smtp_host, smtp_port, username, password
                 optional: smtp_security (starttls | ssl | none, default starttls), imap_security (ssl | none, default ssl),
                           from_address (default: username), mailbox (default INBOX)
    allowed_user_ids: the e-mail addresses that may talk to it

The inbox is polled every `email.poll_interval_s` (30) seconds over IMAP; each unread message from an allowed
address is answered by the agent and the answer is sent by SMTP as a reply (same subject, `In-Reply-To` / `References`
set, so the person's mail client keeps it in the thread). Each sender is a separate conversation.

**An e-mail's From address is trivially forged.** Being on the allow-list is therefore not enough by itself: the message
must also carry an `Authentication-Results` header - added by *your* mail provider when it received the mail - saying
SPF, DKIM or DMARC passed for the sender's domain (`email.require_authentication: true`, the default). A message
without one is ignored and audited. Turn it off only for a mailbox on a private server you control. What the message
says is the sender's own instruction to their own agent, exactly like a chat message; a forwarded mail from a
stranger is still the allowed person's text, so tell the agent nothing you would not tell them.

Also refused, to avoid mail loops and spam storms: automatic replies and bulk mail (`Auto-Submitted`, `Precedence:
bulk|list|junk`, `List-Id`, `X-Autoreply`), mail from the bot's own address, and more than `email.max_replies_per_hour`
(20) replies to one sender. Attachments are ignored; only the text is read (HTML is reduced to text), quoted earlier
messages are cut, and the text is limited to 8000 characters.

Tested against small in-process IMAP and SMTP servers written for the tests; **not tested against Gmail, Outlook or any
real provider**. Providers that require OAuth instead of a password (Gmail, Microsoft 365 without app passwords) are not
supported.
"""
from __future__ import annotations

import asyncio
import email
import email.policy
import imaplib
import logging
import re
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from html.parser import HTMLParser
from typing import Any, Optional

from bot.platforms import _relay

logger = logging.getLogger("bot.platforms.email")

MAX_BODY = 8000
_replies: dict[tuple, list[float]] = {}


def _cfg() -> dict:
    try:
        from bot.config import config

        return (config.current.get("email")) or {}
    except Exception:  # noqa: BLE001
        return {}


@dataclass
class Incoming:
    sender: str
    display: str
    subject: str
    message_id: str
    references: str
    body: str
    authenticated: bool
    automatic: bool
    uid: str = ""


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


_QUOTE_HEAD = re.compile(r"(?im)^\s*(on .{5,120} wrote:|-{2,}\s*original message\s*-{2,}|from:\s.+\n\s*sent:\s.+)\s*$")


def strip_quoted(text: str) -> str:
    """The part of a reply the person actually wrote: cut at 'On <date>, X wrote:' and drop '>' quote lines and the signature."""
    m = _QUOTE_HEAD.search(text)
    if m:
        text = text[:m.start()]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    for i, ln in enumerate(lines):
        if ln.strip() == "--":
            lines = lines[:i]
            break
    return "\n".join(lines).strip()


def body_text(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_type() == "text/html":
        parser = _Text()
        parser.feed(content)
        content = "".join(parser.parts)
    return strip_quoted(re.sub(r"[ \t]+\n", "\n", content))[:MAX_BODY]


def passed_authentication(msg) -> bool:
    """True if the receiving server recorded a pass for SPF, DKIM or DMARC. (The header is added by the mail
    provider; a sender cannot be trusted to supply their own, so only the *top-most* one - the provider's - is read.)"""
    values = msg.get_all("Authentication-Results") or []
    if not values:
        return False
    first = str(values[0]).lower()
    return bool(re.search(r"\b(dkim|dmarc|spf)=pass\b", first)) and not re.search(r"\bdmarc=fail\b", first)


def is_automatic(msg) -> bool:
    if str(msg.get("Auto-Submitted", "no")).strip().lower() not in ("no", ""):
        return True
    if str(msg.get("Precedence", "")).strip().lower() in ("bulk", "list", "junk"):
        return True
    return any(msg.get(h) for h in ("List-Id", "List-Unsubscribe", "X-Autoreply", "X-Autorespond"))


def parse(raw: bytes) -> Incoming:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, address = parseaddr(str(msg.get("From", "")))
    return Incoming(sender=_relay.normalise_email(address), display=name, subject=str(msg.get("Subject", "")).strip(),
                    message_id=str(msg.get("Message-ID", "")).strip(), references=str(msg.get("References", "")).strip(),
                    body=body_text(msg), authenticated=passed_authentication(msg), automatic=is_automatic(msg))


# ---- IMAP / SMTP (blocking; run in a thread) ----------------------------------------------------------------
def _imap(creds: dict):
    port = int(creds.get("imap_port") or 993)
    if str(creds.get("imap_security", "ssl")).lower() == "none":
        client = imaplib.IMAP4(creds["imap_host"], port, timeout=30)
    else:
        client = imaplib.IMAP4_SSL(creds["imap_host"], port, timeout=30)
    client.login(creds["username"], creds["password"])
    return client


def fetch_unseen(creds: dict, limit: int = 20) -> list[Incoming]:
    """Unread messages, marked read once fetched (a message that fails to parse is still marked, so it is not retried forever)."""
    client = _imap(creds)
    out: list[Incoming] = []
    try:
        client.select(creds.get("mailbox") or "INBOX")
        status, data = client.search(None, "UNSEEN")
        ids = data[0].split()[:limit] if status == "OK" and data and data[0] else []
        for num in ids:
            status, parts = client.fetch(num, "(BODY.PEEK[])")
            client.store(num, "+FLAGS", "\\Seen")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            try:
                item = parse(parts[0][1])
                item.uid = num.decode()
                out.append(item)
            except Exception:  # noqa: BLE001
                logger.warning("could not parse an incoming message", exc_info=True)
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


def build_reply(creds: dict, to: str, subject: str, body: str, *, in_reply_to: str = "", references: str = "") -> EmailMessage:
    msg = EmailMessage()
    sender = creds.get("from_address") or creds["username"]
    msg["From"] = formataddr(("", sender))
    msg["To"] = to
    subject = subject.strip() or "Message from your bot"
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg["Auto-Submitted"] = "auto-replied"                 # so a mail robot on the other side does not answer us back
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip()
    msg.set_content(body)
    return msg


def send_message(creds: dict, msg: EmailMessage) -> None:
    security = str(creds.get("smtp_security", "starttls")).lower()
    port = int(creds.get("smtp_port") or (465 if security == "ssl" else 587))
    if security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(creds["smtp_host"], port, timeout=30)
    else:
        smtp = smtplib.SMTP(creds["smtp_host"], port, timeout=30)
    try:
        smtp.ehlo()
        if security == "starttls":
            smtp.starttls()
            smtp.ehlo()
        smtp.login(creds["username"], creds["password"]) if creds.get("password") and security != "none" else None
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:  # noqa: BLE001
            pass


# ---- one message ---------------------------------------------------------------------------------------------
async def process(instance: dict, item: Incoming, *, now: Optional[float] = None) -> str:
    """Handle one incoming message. Returns what happened: "answered", or why it was ignored."""
    creds = instance["credentials"]
    own = _relay.normalise_email(creds.get("from_address") or creds["username"])
    if item.sender == own or item.automatic:
        return "ignored: automatic or from itself"
    if not _relay.is_allowed(instance, item.sender, _relay.normalise_email):
        _relay.reject(instance, "email", item.sender)
        return "ignored: sender not allowed"
    if str(_cfg().get("require_authentication", True)).lower() != "false" and not item.authenticated:
        db_audit(instance, item.sender, "no passing SPF/DKIM/DMARC result from the receiving server")
        return "ignored: sender not authenticated"
    limit = int(_cfg().get("max_replies_per_hour", 20))
    moment = time.time() if now is None else now
    recent = [t for t in _replies.get((instance["id"], item.sender), []) if moment - t < 3600]
    if len(recent) >= limit:
        return "ignored: too many replies to this sender this hour"
    _replies[(instance["id"], item.sender)] = recent + [moment]
    text = item.body or item.subject          # a mail with only a subject line is a short message

    async def send(chat_id: str, reply: str) -> None:
        msg = build_reply(creds, item.sender, item.subject, reply, in_reply_to=item.message_id, references=item.references)
        await asyncio.to_thread(send_message, creds, msg)

    await _relay.relay(instance, "email", item.sender, item.sender, text, send, username=item.display)
    return "answered"


def db_audit(instance: dict, sender: str, why: str) -> None:
    from bot import db

    db.log_audit(actor=sender, action="email_rejected", detail=f"{why} (instance {instance['id']})")


async def run_instance(row: dict[str, Any]) -> None:
    """The supervisor's task: poll the inbox until cancelled."""
    from bot import outbox

    creds = row["credentials"]

    async def _send(chat_id: Any, text: str) -> None:
        await asyncio.to_thread(send_message, creds, build_reply(creds, str(chat_id), "Message from your bot", text))

    outbox.register(row["id"], _send)
    interval = max(5.0, float(_cfg().get("poll_interval_s", 30)))
    try:
        while True:
            try:
                for item in await asyncio.to_thread(fetch_unseen, creds):
                    try:
                        await process(row, item)
                    except Exception:  # noqa: BLE001 - one bad message must not stop the inbox
                        logger.exception("email message handling failed")
            except (imaplib.IMAP4.error, OSError) as exc:
                logger.warning("email poll failed for %r: %s", row["name"], exc)
            await asyncio.sleep(interval)
    finally:
        outbox.unregister(row["id"])

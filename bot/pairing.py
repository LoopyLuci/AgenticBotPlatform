"""Pairing codes for an unrecognized chat-platform user — lets someone who
isn't yet on a bot instance's allowlist ask the owner to add them, instead
of the bot just silently ignoring them forever.

Modeled on the real Hermes Agent's gateway/pairing.py (confirmed by reading
that source directly): short unambiguous codes, a TTL, a rate limit per
user, and a cap on how many codes can be pending at once — the same
constants Hermes uses, since they're sane defaults, not something specific
to Hermes's own storage. Approval there is CLI-only; here it's a dashboard
action (bot/dashboard/server.py's /api/pairing endpoints) since BotServer
has no CLI a chat-bot owner would reach for — approving a specific,
already-issued code from a list is unambiguous, so there's no equivalent to
Hermes's "5 failed manual code entries -> lockout" (there's no free-text
code entry here to get wrong).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot import bot_instances, db

# No 0/O/1/I — visually ambiguous in most fonts, same alphabet Hermes uses.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
TTL_SECONDS = 3600
RATE_LIMIT_SECONDS = 600  # 1 request per user per instance per 10 minutes
MAX_PENDING_PER_USER = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))


def request_code(instance_id: int, user_id, user_name: str, chat_id) -> tuple[bool, str]:
    """Issues a new pairing code for (instance_id, user_id), or refuses with
    a user-facing reason if the requester is rate-limited or already has
    too many codes pending. Returns (ok, code_or_message)."""
    since_rate = _iso(_now() - timedelta(seconds=RATE_LIMIT_SECONDS))
    if db.count_recent_pairing_requests(instance_id, user_id, since_rate) > 0:
        return False, "Too many pairing requests right now — please try again later."

    if db.count_pending_pairing_codes(instance_id, user_id=user_id) >= MAX_PENDING_PER_USER:
        return False, "You already have a pairing request pending — ask the bot owner to approve it."

    code = _generate_code()
    while db.get_pairing_code(code) is not None:  # astronomically unlikely, cheap to guard anyway
        code = _generate_code()

    expires_at = _iso(_now() + timedelta(seconds=TTL_SECONDS))
    db.create_pairing_code(instance_id, code, str(user_id), user_name or "", str(chat_id), expires_at)
    return True, code


def list_pending(instance_id: Optional[int] = None) -> list[dict]:
    rows = db.list_pending_pairing_codes(instance_id)
    return [dict(r) for r in rows]


def approve(pairing_id: int, actor: str = "dashboard") -> dict:
    """Approves a pending pairing request: appends the requester's user id
    into the owning instance's allowed_user_ids and marks the code used.
    Raises ValueError if the code doesn't exist or already has an outcome."""
    row = db.get_pairing_code_by_id(pairing_id)
    if row is None:
        raise ValueError(f"pairing request {pairing_id} not found")
    if row["approved_at"] or row["denied_at"]:
        raise ValueError("this pairing request was already resolved")

    instance = bot_instances.get_instance(row["instance_id"])
    if instance is None:
        raise ValueError(f"bot instance {row['instance_id']} no longer exists")

    user_id = row["user_id"]
    allowed = list(instance["allowed_user_ids"])
    # allowed_user_ids is typically a list of ints for telegram/discord —
    # match bot_instances._validate_allowed_ids' own numeric convention
    # when the platform is one of those, falling back to the raw string
    # (Slack ids are strings) rather than guessing wrong either way.
    candidate: object = user_id
    if instance["platform"] in ("telegram", "discord"):
        try:
            candidate = int(user_id)
        except (TypeError, ValueError):
            candidate = user_id
    if candidate not in allowed:
        allowed.append(candidate)
        bot_instances.update_instance(row["instance_id"], allowed_user_ids=allowed, actor=actor)

    db.approve_pairing_code(pairing_id)
    db.log_audit(actor=actor, action="pairing_approve", detail=f"instance {row['instance_id']} user {user_id}")
    return dict(row)


def deny(pairing_id: int, actor: str = "dashboard") -> dict:
    row = db.get_pairing_code_by_id(pairing_id)
    if row is None:
        raise ValueError(f"pairing request {pairing_id} not found")
    if row["approved_at"] or row["denied_at"]:
        raise ValueError("this pairing request was already resolved")
    db.deny_pairing_code(pairing_id)
    db.log_audit(actor=actor, action="pairing_deny", detail=f"instance {row['instance_id']} user {row['user_id']}")
    return dict(row)

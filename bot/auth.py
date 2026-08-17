"""Single-user (or short allowlist) auth for the Telegram bot.

Every update is checked against ALLOWED_TELEGRAM_USER_IDS from .env, plus
any users added at runtime via the dashboard (stored in db.allowed_users).
Anything else is dropped and logged — never answered, never acknowledged.
"""

from __future__ import annotations

import logging
import os

from bot import db

logger = logging.getLogger("bot.auth")


def _env_ids() -> set[int]:
    raw = os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                logger.warning("ignoring non-numeric ALLOWED_TELEGRAM_USER_IDS entry: %r", part)
    return ids


def is_allowed(user_id: int) -> bool:
    return user_id in list_allowed_ids()


def list_allowed_ids() -> set[int]:
    try:
        db_ids = {row["telegram_id"] for row in db.list_allowed_users()}
    except Exception:
        db_ids = set()
    return _env_ids() | db_ids


def reject_and_log(user_id: int, username: str = "") -> None:
    logger.warning("rejected message from unauthorized user %s (%s)", user_id, username)
    try:
        db.log_audit(actor=str(user_id), action="unauthorized_attempt", detail=username)
    except Exception:
        pass

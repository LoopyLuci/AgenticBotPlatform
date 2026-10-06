"""What a rule gets to see.

A rule never sees ABP: it sees a flat map of names to plain values, built here, and nothing else. That
is deliberate. It means the whole surface of the language is knowable in one screenful, it means a
rule cannot reach the database or another person's conversation by accident, and it means a rule
written today still means the same thing when the context grows a key next year.

    message     the text of this message or prompt
    sender      who sent it - a name, a handle, or an id as text
    sender_id   their id, when there is one
    platform    telegram | discord | sms | dashboard | scheduled | ...
    action_type quick_question | scheduled | tool_result | ...
    instance_id the bot instance this turn is for
    bot         that instance's name
    tags        a list of strings attached to this turn
    hour        0-23, local time        minute     0-59, local time
    weekday     0 is Monday             date       YYYY-MM-DD, local
    time        HH:MM, local            timestamp  seconds since the epoch

Both `ctx.message` and a bare `message` are the same string: the whole map is bound as `ctx`, and
every top-level key of it is also bound under its own name. `ctx.` is the form to write, because it
is the one that cannot collide with a function name.

The time values are supplied here, not read by the rule: the sandbox has no clock, so "only run this
after 17:00" has to be answered from something ABP decided rather than from the machine the rule
happened to land on.

Everything here must be plain data - text, a number, a boolean, a list, a map - because that is all
the engine accepts, and it says so by name rather than quietly stringifying something.
"""
from __future__ import annotations

import time as _time
from datetime import datetime
from typing import Any, Optional

# Anything longer than this in one context value is refused by the engine; trimming here means the
# rule sees a whole sentence instead of failing on a pasted log.
MAX_VALUE_CHARS = 32_000
MAX_ITEMS = 512


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)[:MAX_VALUE_CHARS]


def _plain(value: Any, depth: int = 0) -> Any:
    """Coerce whatever a caller passed into something the engine can hold. Anything it cannot hold
    becomes its text rather than disappearing, so a rule that looks at it sees what actually happened."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_VALUE_CHARS]
    if isinstance(value, dict):
        if depth >= 6:
            return _text(value)[:MAX_VALUE_CHARS]
        return {_name(str(k))[:64]: _plain(v, depth + 1) for k, v in list(value.items())[:MAX_ITEMS]}
    if isinstance(value, (list, tuple, set, frozenset)):
        if depth >= 6:
            return _text(list(value))[:MAX_VALUE_CHARS]
        return [_plain(v, depth + 1) for v in list(value)[:MAX_ITEMS]]
    return _text(value)


def _name(key: str) -> str:
    """A context key a rule could read: letters, digits and underscore, never starting with a digit."""
    cleaned = "".join(c if (c.isalnum() or c == "_") else "_" for c in key)
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"k{cleaned}"
    return cleaned


def when(now: Optional[datetime] = None) -> dict:
    """The clock fields, from ABP's own local time. Split out so a caller can say which moment it
    means rather than the rule guessing."""
    moment = now or datetime.now()
    return {
        "hour": moment.hour,
        "minute": moment.minute,
        "weekday": moment.weekday(),                 # 0 is Monday, which is what Python already uses
        "weekend": moment.weekday() >= 5,
        "date": moment.strftime("%Y-%m-%d"),
        "time": moment.strftime("%H:%M"),
        "timestamp": int(_time.time()),
    }


def build(*, message: Any = "", sender: Any = "", sender_id: Any = None, platform: Any = "",
          action_type: Any = "", instance_id: Optional[int] = None, bot: Any = "", tags: Any = None,
          now: Optional[datetime] = None, extra: Optional[dict] = None) -> dict:
    """The context one rule is evaluated against. `extra` adds names this list does not have (a hook
    event, a tool name, a model), which is how the same rule works in more than one place."""
    context: dict = {
        "message": _text(message),
        "sender": _text(sender),
        "sender_id": sender_id if isinstance(sender_id, (int, float)) or sender_id is None else _text(sender_id),
        "platform": _text(platform),
        "action_type": _text(action_type),
        "instance_id": int(instance_id) if isinstance(instance_id, (int, float)) else 0,
        "bot": _text(bot),
        "tags": _plain(list(tags)) if tags else [],
    }
    context.update(when(now))
    for key, value in (extra or {}).items():
        context[_name(key)] = _plain(value)
    return context


def from_message(text: Any, *, sender: Any = "", sender_id: Any = None, platform: Any = "",
                 action_type: Any = "", instance_id: Optional[int] = None, bot: Any = "",
                 tags: Any = None, now: Optional[datetime] = None, extra: Optional[dict] = None) -> dict:
    """The same map, named for the usual case: a message arriving somewhere."""
    return build(message=text, sender=sender, sender_id=sender_id, platform=platform,
                 action_type=action_type, instance_id=instance_id, bot=bot, tags=tags, now=now, extra=extra)


def from_event(event: str, payload: Optional[dict] = None, *, instance_id: Optional[int] = None,
               matcher: Any = "", now: Optional[datetime] = None, extra: Optional[dict] = None) -> dict:
    """The same map for a hook event: the event name and whatever the event carries become context,
    so a filter can ask `ctx.event == "PreToolUse" && ctx.tool == "Bash"`."""
    fields = {"event": event, "matcher": matcher}
    fields.update(payload or {})
    fields.update(extra or {})
    return build(message=fields.get("message", ""), sender=fields.get("sender", ""),
                 sender_id=fields.get("sender_id"), platform=fields.get("platform", ""),
                 action_type=fields.get("action_type", "tool"), instance_id=instance_id,
                 bot=fields.get("bot", ""), tags=fields.get("tags"), now=now, extra=fields)


def instance_name(instance_id: Optional[int]) -> str:
    """A bot instance's name for the context, or "" when there is none or it is gone. Best effort by
    design: building a rule's context must never be the reason a turn fails."""
    if instance_id is None:
        return ""
    try:
        from bot import bot_instances

        row = bot_instances.get_instance(instance_id)
        return str((row or {}).get("name") or "")
    except Exception:  # noqa: BLE001
        return ""
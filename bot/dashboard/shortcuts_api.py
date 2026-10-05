"""Keyboard shortcuts API (roadmap P7): the keyboard shortcuts, command palette and right-click menus
for both ABP UIs, and where a person's own bindings are kept.

    GET    /api/shortcuts                     every saved binding (action id -> chord, "" = unbound)
    PUT    /api/shortcuts    {bindings}       replace the saved set
    POST   /api/shortcuts/check {chord}       is this chord readable, unreserved and free right now?
    DELETE /api/shortcuts                     forget every custom binding, back to the defaults

Both UIs load the same registry (bot/dashboard/static/action-registry.js, byte-identical to
desktop-app/ui/action-registry.js) and the same engine (shortcuts.js beside it). This module owns
only the persistence: a person's bindings are a preference about this ABP install, not about one
browser, so they live in config/backends.yaml under `shortcuts.bindings` next to every other ABP
setting - which means they follow the person to another browser, another device or the desktop app.

Reading needs the normal dashboard auth, so a paired phone can see what is bound; writing needs the
dashboard token itself, because a binding is a standing instruction to this ABP to act without
being asked again (and a hostile binding is how you get an emergency stop on a single keystroke).
Validation happens here rather than only in the browser because this is the real gate: a chord
that already belongs to the browser or the OS is refused, and two actions cannot share a chord -
the second one would silently never fire. Both routes are registered once, at startup, so this
module is on bot/hotreload.py's denylist.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException

# Chords the browser, the OS or the window manager already own. Mirrored in shortcuts.js's RESERVED
# (tests/test_shortcuts.py fails if the two lists drift): binding any of these would either break
# the browser's own shortcut or be fought over by the browser, and neither is worth a convenience.
RESERVED: frozenset[str] = frozenset({
    "mod+t", "mod+n", "mod+w", "mod+q", "mod+l", "mod+r", "mod+s", "mod+o", "mod+p", "mod+f",
    "mod+shift+n", "mod+shift+t", "mod+shift+w", "mod+shift+q", "mod+shift+s", "mod+shift+p",
    "mod+alt+l", "mod+alt+d", "mod+alt+f4", "mod+tab", "mod+space", "f5", "f6", "f11", "f12",
    "alt+tab", "alt+f4", "ctrl+shift+i", "ctrl+shift+j", "ctrl+shift+c", "escape",
})

# One token is modifiers joined by + and one key; a chord is up to three such tokens ("g then vi").
_TOKEN = re.compile(r"^(?:(?:mod|ctrl|alt|shift|meta)\+)*[a-z0-9`/?.,;'\"\[\]{}\\-]+$")
_ACTION_ID = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_MAX_TOKENS = 3
MAX_BINDINGS = 200


def is_reserved(chord: str) -> bool:
    """Is any of this chord's tokens a key the browser, the OS or the window manager already owns?

    Checked token by token because a sequence only needs one of its steps to be somebody else's:
    `g` then `Ctrl+T` is still Ctrl+T.
    """
    return any(token in RESERVED for token in str(chord or "").split(" ") if token)


class ShortcutError(ValueError):
    """A binding that must not be saved: bad syntax, a reserved chord, or a conflict."""


def normalize(chord: str) -> str:
    """Canonical form of a chord, so Ctrl and ctrl, and Cmd and Ctrl, compare equal.

    `cmd`/`ctrl` both become `mod` (the engine resolves `mod` per platform) and `opt` becomes
    `alt`. Tokens are lowercased and trimmed; the order of tokens is kept because "g then vi" and
    "vi then g" are different chords.
    """
    text = str(chord or "").strip().lower()
    if not text:
        return ""
    out: list[str] = []
    for raw in text.split():
        parts = [p for p in raw.split("+") if p != ""]
        if not parts:
            continue
        parts = ["mod" if p in ("cmd", "ctrl") else "alt" if p in ("opt", "option") else "escape" if p == "esc" else p for p in parts]
        token = "+".join(parts)
        if token in ("mod", "ctrl", "alt", "shift", "meta") or not _TOKEN.match(token):
            raise ShortcutError(f"{chord!r} is not a shortcut ABP understands: each key is a name, a letter, a digit or a symbol")
        out.append(token)
    if not out:
        return ""
    if len(out) > _MAX_TOKENS:
        raise ShortcutError(f"{chord!r} is too long: a shortcut is at most {_MAX_TOKENS} keys in sequence")
    return " ".join(out)


def validate(bindings: dict) -> dict:
    """The whole saved set, checked together, so a conflict is caught in the same PUT that caused it.

    Raises ShortcutError on the first problem, naming the action and the chord, because a silent
    second-wins-here rule is how somebody spends an afternoon wondering why their key stopped
    working. An empty string is a deliberate "no shortcut for this one" and is kept as-is.
    """
    if not isinstance(bindings, dict):
        raise ShortcutError("payload must be {\"bindings\": {\"action.id\": \"mod+k\", ...}}")
    if len(bindings) > MAX_BINDINGS:
        raise ShortcutError(f"too many bindings: {len(bindings)} (the limit is {MAX_BINDINGS})")
    clean: dict[str, str] = {}
    owner: dict[str, str] = {}
    for raw_id, raw_chord in bindings.items():
        action_id = str(raw_id).strip()
        if not _ACTION_ID.match(action_id):
            raise ShortcutError(f"{raw_id!r} is not an action id (lowercase words, dot or dash separated)")
        chord = normalize(raw_chord)
        if not chord:
            clean[action_id] = ""
            continue
        if is_reserved(chord):
            raise ShortcutError(f"{chord} belongs to the browser or the OS and cannot be taken")
        if chord in owner:
            raise ShortcutError(f"{chord} is already bound to {owner[chord]}")
        owner[chord] = action_id
        clean[action_id] = chord
    return clean


def stored() -> dict:
    """The saved bindings, or {} when there are none (or config is unreadable - defaults still work)."""
    from bot.config import config

    raw = (config.current.get("shortcuts") or {}).get("bindings")
    if not isinstance(raw, dict):
        return {}
    try:
        return validate(raw)
    except ShortcutError:
        # A hand-edited backends.yaml should not break the keyboard for everybody; fall back to
        # defaults and let the customiser overwrite it.
        return {}


def save(bindings: dict) -> dict:
    """Check, then write the whole set at once. Stored like any other ABP setting, comments intact."""
    from bot.config import config

    clean = validate(bindings)
    config.set_values({("shortcuts", "bindings"): clean}, actor="dashboard")
    return clean


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    def _audit(action: str, detail: str) -> None:
        from bot import db

        try:
            db.log_audit(actor="dashboard", action=action, detail=detail[:500])
        except Exception:  # noqa: BLE001 - the audit log must never be the reason a key stops working
            pass

    @app.get("/api/shortcuts", dependencies=read)
    async def get_shortcuts() -> dict:
        """Every saved binding, keyed by action id. An empty object means "all defaults"; an empty
        string value means that action was deliberately unbound."""
        return {"bindings": stored()}

    @app.put("/api/shortcuts", dependencies=write)
    async def put_shortcuts(payload: dict = Body(...)):
        """Replace the saved set with `{"bindings": {"action.id": "mod+k"}}`.

        The whole set is checked before anything is written, so a rejected PUT leaves the existing
        bindings exactly as they were. A 400 means one of: the chord is not something ABP can read,
        it belongs to the browser or the OS, or two actions would end up on the same chord.
        """
        raw = payload.get("bindings")
        if raw is None:
            raise HTTPException(status_code=400, detail="payload must be {\"bindings\": {...}}")
        try:
            clean = save(raw)
        except ShortcutError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit("shortcuts_save", f"{len(clean)} binding(s): {', '.join(f'{k}={v}' for k, v in sorted(clean.items()))[:400]}")
        return {"bindings": clean}

    @app.delete("/api/shortcuts", dependencies=write)
    async def delete_shortcuts():
        """Forget every custom binding; both UIs go back to the registry's defaults."""
        from bot.config import config

        config.set_values({("shortcuts", "bindings"): {}}, actor="dashboard")
        _audit("shortcuts_reset", "all custom bindings removed")
        return {"bindings": {}}

    @app.post("/api/shortcuts/check", dependencies=write)
    async def check_shortcut(chord: str = Body(..., embed=True), bindings: Optional[dict] = Body(default=None, embed=True)):
        """Ask before you save: is this chord readable, unreserved and free?

        The customiser calls this as you press a key so a conflict shows up while you are looking
        at it. It checks against the bindings passed in `bindings` (what the page currently has)
        or the saved set when none are given.
        """
        against = bindings if isinstance(bindings, dict) else stored()
        try:
            wanted = normalize(chord)
        except ShortcutError as exc:
            return {"ok": False, "reason": str(exc)}
        if not wanted:
            return {"ok": True, "chord": ""}
        if is_reserved(wanted):
            return {"ok": False, "chord": wanted, "reason": f"{wanted} belongs to the browser or the OS"}
        # Anything already on this chord makes the new binding ambiguous. Screen-scoped actions on
        # different screens may share a chord (the three Mod+Enter composers), which validate()
        # allows because the engine picks by screen; this endpoint cannot see the registry's
        # contexts, so it only reports the cases it is certain about.
        for action_id, value in against.items():
            if normalize(value) == wanted:
                return {"ok": False, "chord": wanted, "reason": f"{wanted} is already bound to {action_id}"}
        return {"ok": True, "chord": wanted}
"""Integration keys: least-privilege API keys for other programs that drive ABP (octopus-router's Bot Platform
view first), instead of the dashboard token, which is root on this machine.

An integration key is an api_keys row of kind "integration" plus a list of scopes (table integration_scopes).
Every request made with one is checked against its scopes' route allowlist before any route runs
(dashboard/server.py _identify_caller), so a scope is the whole of what the key can reach, and bot rows are
always served to it with their credentials removed.

Also here: the framing policy. `integrations.frame_ancestors` (config/backends.yaml) lists the sites allowed to
show ABP's dashboard in a frame (the Router's origin), and `integrations.frame_src` the sites ABP's own panes may
show; both are added to the page's Content-Security-Policy.
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterable, Optional
from urllib.parse import urlparse

from bot import db

KIND = "integration"

# scope -> [(method, path regex)]. Paths are matched in full.
SCOPES: dict[str, list[tuple[str, str]]] = {
    "status:read": [("GET", r"/healthz"), ("GET", r"/api/overview"), ("GET", r"/api/integrations/whoami")],
    "bots:read": [("GET", r"/api/bots"), ("GET", r"/api/bots/\d+")],
    "bots:control": [("POST", r"/api/bots/[A-Za-z0-9_.-]{1,128}/(start|stop|restart|enable|disable)")],
    "docker:read": [("GET", r"/api/infra/hosts"), ("GET", r"/api/docker(/[A-Za-z0-9_.:/-]*)?")],
    "docker:control": [("POST", r"/api/docker/(containers|stacks)/[A-Za-z0-9_.-]{1,128}/action")],
    "modules:read": [("GET", r"/api/modules(/[A-Za-z0-9_.-]+)?(/(operations|status|conformance))?")],
    "modules:call": [("POST", r"/api/modules/[A-Za-z0-9_.-]+/call")],
    "cluster:read": [("GET", r"/api/cluster/(status|nodes|jobs)(/[A-Za-z0-9_.-]+)?")],
    "models:read": [("GET", r"/api/models"), ("GET", r"/api/providers")],
}

PRESETS: dict[str, dict] = {
    "octopus-router": {
        "label": "octopus-router (Bot Platform view)",
        "scopes": ["status:read", "bots:read", "bots:control", "docker:read", "docker:control", "modules:read"],
        "note": "What octopus-router's server/botplatform.js calls: bots and their lifecycle, Docker read and "
                "start/stop/restart/pause/unpause/pull/up. No destructive Docker verbs, no config, no credentials.",
    },
    "read-only": {"label": "read-only monitor", "scopes": ["status:read", "bots:read", "docker:read", "modules:read",
                                                            "cluster:read", "models:read"]},
}

_COMPILED = {s: [(m, re.compile(p + r"/?")) for m, p in rules] for s, rules in SCOPES.items()}


def _conn():
    conn = db.get_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS integration_scopes (api_key_id INTEGER PRIMARY KEY, scopes TEXT NOT NULL, "
                 "origin TEXT NOT NULL DEFAULT '', preset TEXT NOT NULL DEFAULT '')")
    return conn


def validate_scopes(scopes: Iterable[str]) -> list[str]:
    out = sorted({str(s) for s in scopes})
    bad = [s for s in out if s not in SCOPES]
    if bad:
        raise ValueError(f"unknown scope(s): {', '.join(bad)} (known: {', '.join(SCOPES)})")
    if not out:
        raise ValueError("an integration key needs at least one scope")
    return out


def normalize_origin(origin: str) -> str:
    """'http://host:port' with no path, or '' for none."""
    origin = (origin or "").strip().rstrip("/")
    if not origin:
        return ""
    u = urlparse(origin)
    if u.scheme not in ("http", "https") or not u.netloc or u.path not in ("", "/") or u.query or "*" in u.netloc:
        raise ValueError(f"{origin!r} is not an origin like https://router.example or http://127.0.0.1:3030")
    return f"{u.scheme}://{u.netloc}".lower()


def mint(label: str, scopes: Iterable[str], *, origin: str = "", preset: str = "") -> tuple[int, str]:
    """A new key; the plaintext is returned once and never stored."""
    scopes = validate_scopes(scopes)
    origin = normalize_origin(origin)
    key_id, plaintext = db.create_api_key(label.strip()[:120] or "integration", kind=KIND)
    conn = _conn()
    with db._lock:
        conn.execute("INSERT OR REPLACE INTO integration_scopes (api_key_id, scopes, origin, preset) VALUES (?, ?, ?, ?)",
                     (key_id, " ".join(scopes), origin, preset))
        conn.commit()
    db.log_audit(actor="dashboard", action="integration_key_minted", detail=f"{key_id} {label}: {' '.join(scopes)}")
    return key_id, plaintext


def list_keys() -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT k.id, k.label, k.created_at, k.last_used_at, k.revoked_at, s.scopes, s.origin, s.preset "
                        "FROM api_keys k LEFT JOIN integration_scopes s ON s.api_key_id = k.id WHERE k.kind = ? "
                        "ORDER BY k.created_at DESC", (KIND,)).fetchall()
    return [{"id": r["id"], "label": r["label"], "created_at": r["created_at"], "last_used_at": r["last_used_at"],
             "revoked": bool(r["revoked_at"]), "scopes": (r["scopes"] or "").split(), "origin": r["origin"] or "",
             "preset": r["preset"] or ""} for r in rows]


def revoke(key_id: int) -> None:
    row = db.get_api_key(key_id)
    if row is None or row["kind"] != KIND:
        raise ValueError(f"no integration key {key_id}")
    db.revoke_api_key(key_id)
    db.log_audit(actor="dashboard", action="integration_key_revoked", detail=str(key_id))


def scopes_for(plaintext: str) -> Optional[list[str]]:
    """The scopes of a live integration key, or None if it is not one."""
    if not plaintext:
        return None
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    row = _conn().execute("SELECT s.scopes FROM api_keys k JOIN integration_scopes s ON s.api_key_id = k.id "
                          "WHERE k.key_hash = ? AND k.kind = ? AND k.revoked_at IS NULL", (key_hash, KIND)).fetchone()
    return row["scopes"].split() if row else None


def allows(scopes: Iterable[str], method: str, path: str) -> bool:
    method = method.upper()
    if method == "HEAD":
        method = "GET"
    for s in scopes:
        for m, rx in _COMPILED.get(s, ()):
            if m == method and rx.fullmatch(path):
                return True
    return False


# ---- framing -------------------------------------------------------------------------------------------------

def _origins(key: str) -> list[str]:
    try:
        from bot.config import config
        raw = ((config.current or {}).get("integrations") or {}).get(key) or []
    except Exception:  # noqa: BLE001 - a bad config never breaks page serving
        return []
    out = []
    for o in raw if isinstance(raw, list) else [raw]:
        try:
            n = normalize_origin(str(o))
        except ValueError:
            continue
        if n and n not in out:
            out.append(n)
    return out


def frame_ancestors() -> list[str]:
    """Sites allowed to show ABP in a frame, besides ABP itself."""
    return _origins("frame_ancestors")


def frame_src() -> list[str]:
    """Sites ABP's panes may show in a frame, besides ABP itself (the Router's origin, estate apps)."""
    return _origins("frame_src")

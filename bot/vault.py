"""A small credential vault for the browser agent (roadmap P6). **The agent never sees a stored secret.**

A person adds a login (a name, the site it belongs to, a username, a password, optionally a TOTP secret for
two-factor codes). The agent can list the *names* and sites (`vault_list`) and ask the browser to fill a field from
one (`browser_act` action `fill_credential`); the browser code reads the secret, checks that the page is the site the
entry belongs to, and types it into the field. The value is never returned to the model, never put in a tool
result, and (see secrets_guard.py) is redacted from any output and blocked from being sent anywhere by the agent
if it does turn up.

Storage: `<data>/vault.enc`, encrypted with Fernet (AES-128-CBC + HMAC). The key comes from the environment
variable `ABP_VAULT_KEY` if set (a urlsafe-base64 32-byte key), otherwise from `<data>/vault.key`, which is created
on first use. **Be clear about what that protects:** with the key file next to the vault, anyone who can read the
folder can read the vault; the encryption keeps passwords out of backups, casual greps and the database, not out of
reach of an attacker who already owns the machine. Set `ABP_VAULT_KEY` (from your OS keychain, for example) to keep
the key elsewhere.

    python -m bot.vault add github --origin https://github.com --username me      # prompts for the password
    python -m bot.vault list
    python -m bot.vault remove github
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import struct
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

FIELDS = ("username", "password", "totp")


class VaultError(Exception):
    pass


def _dir() -> Path:
    from bot.envfile import PROJECT_ROOT

    explicit = os.environ.get("ABP_VAULT_DIR", "").strip()
    path = Path(explicit) if explicit else PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fernet() -> Fernet:
    env = os.environ.get("ABP_VAULT_KEY", "").strip()
    if env:
        try:
            return Fernet(env.encode())
        except ValueError as exc:
            raise VaultError("ABP_VAULT_KEY is not a valid Fernet key (32 urlsafe-base64 bytes)") from exc
    key_file = _dir() / "vault.key"
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
    return Fernet(key_file.read_bytes().strip())


def origin_of(url: str) -> str:
    """scheme://host[:port] in lower case - what a credential is bound to."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise VaultError("an origin must be an http(s) address such as https://github.com")
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    return f"{parts.scheme}://{parts.hostname.lower()}{port}"


def _load() -> dict:
    path = _dir() / "vault.enc"
    if not path.exists():
        return {}
    try:
        return json.loads(_fernet().decrypt(path.read_bytes()).decode("utf-8"))
    except (InvalidToken, ValueError) as exc:
        raise VaultError("the vault cannot be decrypted (wrong or missing key?)") from exc


def _save(data: dict) -> None:
    path = _dir() / "vault.enc"
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(_fernet().encrypt(json.dumps(data).encode("utf-8")))
    os.replace(tmp, path)


def add(name: str, *, origin: str, username: str = "", password: str = "", totp_secret: str = "", note: str = "") -> None:
    name = name.strip()
    if not name or len(name) > 60 or not all(c.isalnum() or c in "-_." for c in name):
        raise VaultError("a credential name is letters, digits, - _ . (at most 60)")
    if not (username or password or totp_secret):
        raise VaultError("give at least a username, a password or a TOTP secret")
    if totp_secret:
        totp(totp_secret)                                   # reject a malformed secret now, not at login time
    data = _load()
    data[name] = {"origin": origin_of(origin), "username": username, "password": password, "totp_secret": totp_secret.replace(" ", ""),
                  "note": note[:200], "added": int(time.time())}
    _save(data)


def remove(name: str) -> bool:
    data = _load()
    if name not in data:
        return False
    del data[name]
    _save(data)
    return True


def listing() -> list[dict]:
    """Names, sites and which fields exist. Never a value."""
    return [{"name": n, "origin": e["origin"], "username": e.get("username", ""), "has_password": bool(e.get("password")),
             "has_totp": bool(e.get("totp_secret")), "note": e.get("note", "")} for n, e in sorted(_load().items())]


def value(name: str, field: str, *, page_url: str) -> str:
    """The secret for `field`, only if `page_url` is the site the entry belongs to. Used by the browser code, never
    handed to the model."""
    if field not in FIELDS:
        raise VaultError(f"field must be one of {', '.join(FIELDS)}")
    entry = _load().get(name)
    if entry is None:
        raise VaultError(f"there is no stored credential named {name!r} (vault_list shows the names)")
    try:
        here = origin_of(page_url)
    except VaultError:
        raise VaultError("this page is not an http(s) site")
    if here != entry["origin"]:
        raise VaultError(f"credential {name!r} belongs to {entry['origin']}, not {here}; it is not filled into other sites")
    if field == "totp":
        if not entry.get("totp_secret"):
            raise VaultError(f"credential {name!r} has no two-factor secret")
        return totp(entry["totp_secret"])
    secret = entry.get(field) or ""
    if not secret:
        raise VaultError(f"credential {name!r} has no {field}")
    return secret


def secrets() -> list[tuple[str, str]]:
    """(label, value) for every stored password and TOTP secret, for redaction (secrets_guard). Empty if the vault
    is unreadable - redaction must never make the agent fail."""
    try:
        out = []
        for name, e in _load().items():
            for field in ("password", "totp_secret"):
                if len(e.get(field) or "") >= 6:
                    out.append((f"vault:{name}:{field}", e[field]))
        return out
    except Exception:  # noqa: BLE001
        return []


# ---- one-time codes (RFC 6238) --------------------------------------------------------------
def totp(secret: str, *, at: Optional[float] = None, digits: int = 6, step: int = 30) -> str:
    try:
        key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    except (ValueError, base64.binascii.Error) as exc:
        raise VaultError("the TOTP secret is not valid base32") from exc
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


# ---- command line ------------------------------------------------------------------------------
def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m bot.vault", description="Manage the browser agent's stored logins.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--origin", required=True)
    a.add_argument("--username", default="")
    a.add_argument("--totp-secret", default="", help="base32 secret for two-factor codes (prompted if --totp)")
    a.add_argument("--no-password", action="store_true")
    sub.add_parser("list")
    r = sub.add_parser("remove")
    r.add_argument("name")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "add":
            password = "" if args.no_password else getpass.getpass("Password (input hidden): ")
            add(args.name, origin=args.origin, username=args.username, password=password, totp_secret=args.totp_secret)
            print(f"stored {args.name}")
        elif args.cmd == "list":
            for e in listing():
                print(f"{e['name']}\t{e['origin']}\t{e['username']}\tpassword={'yes' if e['has_password'] else 'no'}\ttotp={'yes' if e['has_totp'] else 'no'}")
        else:
            print("removed" if remove(args.name) else "no such credential")
    except VaultError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Credential vault API (roadmap P6): the Vault screen's routes.

    GET    /api/vault                list: name, origin, username, which kinds exist, when added, and the key's whereabouts
    POST   /api/vault                add one entry (write token); the secret is read once and never returned
    DELETE /api/vault/{name}         remove one entry (write token)

**No response ever contains a password, a TOTP secret or a note.** The list is built from an explicit allow-list of
fields (bot/vault.py `listing()` never holds values, and this module copies only the fields below). The add route
reads the body by hand rather than through a request model, because FastAPI's validation errors echo the offending
input back, and the input here is a secret. Error messages are fixed strings or the vault's own (which never name a value).
"""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Request

_MAX_BODY = 16_384
_SHOWN = ("name", "origin", "username", "has_password", "has_totp", "added")
_KEY_TEXT = {
    "env": "The encryption key comes from the ABP_VAULT_KEY environment variable, so it is not stored beside the vault.",
    "file": "The encryption key is a file kept beside the vault. Anyone who can read the vault's folder can read the vault. "
            "Set ABP_VAULT_KEY (from your operating system's keychain, for example) to keep the key elsewhere.",
    "none": "There is no key yet. One is created in a file beside the vault when you add the first entry. "
            "Set ABP_VAULT_KEY first if you would rather keep the key elsewhere.",
}


def _shown(entry: dict) -> dict:
    return {k: entry.get(k) for k in _SHOWN}


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import vault

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    @app.get("/api/vault", dependencies=read)
    async def vault_list():
        try:
            entries = [_shown(e) for e in await asyncio.to_thread(lambda: vault.listing(with_added=True))]
            readable, error = True, ""
        except vault.VaultError as exc:
            entries, readable, error = [], False, str(exc)
        source = await asyncio.to_thread(vault.key_source)
        return {"entries": entries, "readable": readable, "error": error, "key_source": source, "key_note": _KEY_TEXT[source]}

    @app.post("/api/vault", dependencies=write)
    async def vault_add(request: Request):
        raw = await request.body()
        if len(raw) > _MAX_BODY:
            raise HTTPException(413, "that is too large for a vault entry")
        try:
            import json

            body = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(400, "the request body must be JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "the request body must be a JSON object")
        fields = {}
        for key in ("name", "origin", "username", "password", "totp_secret"):
            v = body.get(key, "")
            if not isinstance(v, str):
                raise HTTPException(400, f"{key} must be text")
            fields[key] = v
        name = fields["name"].strip()
        try:
            if any(e["name"] == name for e in await asyncio.to_thread(vault.listing)):
                raise HTTPException(409, "an entry with that name exists; remove it first to replace it")
            await asyncio.to_thread(
                lambda: vault.add(name, origin=fields["origin"], username=fields["username"], password=fields["password"],
                                  totp_secret=fields["totp_secret"]))
        except vault.VaultError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True, "name": name}

    @app.delete("/api/vault/{name}", dependencies=write)
    async def vault_remove(name: str):
        try:
            removed = await asyncio.to_thread(vault.remove, name)
        except vault.VaultError as exc:
            raise HTTPException(400, str(exc)) from None
        if not removed:
            raise HTTPException(404, "no such entry")
        return {"ok": True}

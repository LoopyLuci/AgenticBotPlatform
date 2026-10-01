"""The agents' file-server tools (bot/fileserver). Reads run freely; anything that changes storage asks the person.

    nas_status       the server, the array (disks, parity, warnings), shares, alerts, jobs, recent events
    nas_disks        every drive: model, health, temperature, SMART, failure risk with reasons; volumes
    nas_search       find files by name, contents, what pictures show, or meaning (the content index)
    nas_duplicates   exact duplicates and near-identical photos, with the space they waste
    nas_locate       where share/path really is on disk (to read it with the file tools)
    nas_share        create / edit / remove a share (asks first)
    nas_array        sync / scrub / fix the parity array (asks first)
    nas_job          run a transfer or backup job now (asks first)
    nas_link         create a share link to a file or folder (asks first)
    nas_guard        unfreeze shares the ransomware guard froze (asks first)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

MAX_OUT = 14_000


def _out(value: Any) -> str:
    text = json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


async def _call(fn, *args, **kwargs) -> str:
    try:
        return _out(await asyncio.to_thread(fn, *args, **kwargs))
    except Exception as e:  # noqa: BLE001 - reaches the agent as text
        return f"Error: {e}" if type(e).__name__ == "FsError" else f"Error: {type(e).__name__}: {e}"


def _locate(share: str, path: str) -> dict:
    from bot.fileserver import shares
    s = shares.get(share)
    hit = shares.locate(s, path)
    if hit:
        return {"share": s["name"], "path": shares.clean(path), "disk": hit[0], "real_path": str(hit[1]), "dir": hit[1].is_dir()}
    disk = shares.emulated(s, path)
    if disk:
        return {"share": s["name"], "path": shares.clean(path), "disk": disk, "real_path": None,
                "note": "its disk is missing: the file server serves it from parity (download it through the file server)"}
    return {"share": s["name"], "path": shares.clean(path), "exists": False}


def _share(inp: dict) -> Any:
    from bot.fileserver import shares
    a = inp.get("action")
    if a == "create":
        return shares.create(inp["name"], inp.get("settings") or {})
    if a == "edit":
        return shares.edit(inp["name"], inp.get("settings") or {})
    if a == "remove":
        return {"removed": shares.remove(inp["name"])}
    raise ValueError("action is create, edit or remove")


def _array(inp: dict) -> Any:
    from bot.fileserver import array
    a = inp.get("action")
    if a == "sync":
        return array.sync()
    if a == "scrub":
        return array.scrub(float(inp.get("percent", 10)))
    if a == "fix":
        return array.fix(inp.get("disk") or None, inp.get("files") or None, inp.get("target") or None)
    raise ValueError("action is sync, scrub or fix")


def _job(inp: dict) -> Any:
    from bot.fileserver import backup, transfer
    if inp.get("kind") == "backup":
        return backup.run_job(inp["name"])
    return transfer.run(inp["name"], dry_run=bool(inp.get("dry_run")))


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    def reg(name, description, props, required, handler, *, read_only=True, needs_approval=None):
        toolspec.register(
            {"name": name, "description": description,
             "input_schema": {"type": "object", "properties": props, "required": required}},
            toolspec.ToolSpec(name, "read" if read_only else "external", read_only=read_only,
                              concurrency_safe=read_only, origin="registered", needs_approval=needs_approval),
            handler)

    async def status(inp, **_):
        from bot.fileserver import service
        return await _call(service.overview, bool(inp.get("deep")))

    async def disks_t(inp, **_):
        from bot.fileserver import disks
        return await _call(disks.inventory)

    async def search(inp, **_):
        from bot.fileserver import index
        return await _call(index.search, inp["query"], [inp["share"]] if inp.get("share") else None, inp.get("mode", "auto"),
                           int(inp.get("limit", 30)), inp.get("kind", ""))

    async def dups(inp, **_):
        from bot.fileserver import index
        return await _call(index.duplicates, [inp["share"]] if inp.get("share") else None)

    async def locate(inp, **_):
        return await _call(_locate, inp["share"], inp.get("path", ""))

    async def share(inp, **_):
        return await _call(_share, inp)

    async def array_t(inp, **_):
        return await _call(_array, inp)

    async def job(inp, **_):
        return await _call(_job, inp)

    async def link(inp, **_):
        from bot.fileserver import shares
        return await _call(shares.create_link, inp["share"], inp.get("path", ""), password=inp.get("password", ""),
                           expires_days=float(inp.get("expires_days", 7)), max_downloads=int(inp.get("max_downloads", 0)),
                           allow_upload=bool(inp.get("allow_upload")), created_by="agent")

    async def guard_t(inp, **_):
        from bot.fileserver import guard
        return await _call(lambda: {"frozen": guard.unfreeze(inp.get("shares"))})

    reg("nas_status", "ABP File Server: whether it runs (its web and WebDAV addresses), the parity array (each disk, parity, "
        "protected bytes, last sync/scrub, pending parity, errors, warnings), cache pools, shares, users, ransomware "
        "alerts and frozen shares, transfer and backup jobs, recent events. deep=true also counts unsynced changes.",
        {"deep": {"type": "boolean"}}, [], status)
    reg("nas_disks", "Every drive on this machine: model, bus, size, health as the OS/SMART reports it, temperature, power-on "
        "hours, and a failure-risk estimate (ok / watch / replace soon) with its reasons; plus every volume's size and free space.",
        {}, [], disks_t)
    reg("nas_search", "Search the file server's shares: by words in names and contents, by what pictures show (objects, faces, "
        "text in them), or by meaning. mode: auto | words | meaning; kind: image, video, audio, document, text, code, archive.",
        {"query": {"type": "string"}, "share": {"type": "string"}, "mode": {"type": "string", "enum": ["auto", "words", "meaning"]},
         "kind": {"type": "string"}, "limit": {"type": "integer"}}, ["query"], search)
    reg("nas_duplicates", "Duplicate files on the shares: exact copies (same SHA-256) and near-identical photos (perceptual "
        "hashes), largest waste first.", {"share": {"type": "string"}}, [], dups)
    reg("nas_locate", "Where a file or folder of a share really is (which disk, the real path), to read it with file tools.",
        {"share": {"type": "string"}, "path": {"type": "string"}}, ["share"], locate)
    reg("nas_share", "Create, edit or remove a file-server share (removing forgets it; files stay). settings: comment, path "
        "(a folder share) or a user share's cache (no|yes|only|prefer), cache_pool, allocation (highwater|mostfree|fillup), "
        "split_level, disks, exclude_disks, access (public|secure|private), users {name: r|rw}, recycle_bin. Asks first.",
        {"action": {"type": "string", "enum": ["create", "edit", "remove"]}, "name": {"type": "string"},
         "settings": {"type": "object"}}, ["action", "name"], share, read_only=False, needs_approval=True)
    reg("nas_array", "Run the parity array's sync (protect new/changed files), scrub (check a percent of it for bitrot) or fix "
        "(rebuild a disk's missing or damaged files; target = a replacement drive's folder). Asks first.",
        {"action": {"type": "string", "enum": ["sync", "scrub", "fix"]}, "percent": {"type": "number"}, "disk": {"type": "string"},
         "files": {"type": "array", "items": {"type": "string"}}, "target": {"type": "string"}}, ["action"], array_t,
        read_only=False, needs_approval=True)
    reg("nas_job", "Run a configured transfer job (copy / mirror / two-way sync; dry_run lists what it would do) or backup job "
        "now. Asks first.", {"kind": {"type": "string", "enum": ["transfer", "backup"]}, "name": {"type": "string"},
                             "dry_run": {"type": "boolean"}}, ["kind", "name"], job, read_only=False, needs_approval=True)
    reg("nas_link", "Create a share link: anyone with the address can open the file or folder (optional password, expiry in "
        "days, download limit, uploads into a folder). Asks first.",
        {"share": {"type": "string"}, "path": {"type": "string"}, "password": {"type": "string"}, "expires_days": {"type": "number"},
         "max_downloads": {"type": "integer"}, "allow_upload": {"type": "boolean"}}, ["share"], link, read_only=False, needs_approval=True)
    reg("nas_guard", "Unfreeze shares the ransomware guard made read-only (all, or the named ones) after the cause is dealt "
        "with. Asks first.", {"shares": {"type": "array", "items": {"type": "string"}}}, [], guard_t, read_only=False, needs_approval=True)


register_tools()

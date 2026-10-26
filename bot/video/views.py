"""How a job and an asset are shown to the API, the agent tools and the Workspace (one shape everywhere)."""
from __future__ import annotations


def asset_view(a: dict | None) -> dict | None:
    if not a:
        return None
    meta = a.get("meta") or {}
    return {"id": a["id"], "url": f"/api/video/assets/{a['id']}/file", "file": a["file"], "bytes": a["bytes"],
            "sha256": a["sha256"], "workflow": a["workflow"], "model": a["model"], "prompt": a["prompt"],
            "seed": a["seed"], "parent_id": a["parent_id"], "job_id": a["job_id"],
            "duration": meta.get("duration"), "size": [meta.get("width"), meta.get("height")],
            "provenance": f"/api/video/assets/{a['id']}/provenance", "created": a["created"]}


def job_view(j: dict | None) -> dict | None:
    if not j:
        return None
    out = {k: j.get(k) for k in ("id", "status", "workflow", "backend", "progress", "message", "error", "attempts",
                                 "created", "started", "finished", "parent_id")}
    out["params"] = j.get("params")
    if j.get("asset"):
        out["asset"] = asset_view(j["asset"])
    return out

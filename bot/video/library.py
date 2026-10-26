"""The asset library: every finished clip in data/video/assets with what made it (prompt, seed, model, workflow
version, backend, job) and where it came from (parent asset: lineage).

Provenance is the likeness module's (bot/likeness/provenance.py), so every generated clip says it is AI-generated the
same three ways: a visible "AI-GENERATED | <asset id>" band on every frame (`video.visible_label`, on by default), the
manifest embedded in the WebM, and a sidecar <file>.provenance.json (C2PA-shaped, unsigned) whose ingredients carry the
parent's SHA-256."""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from bot.video import media, store

VIDEO_EXT = {".webm", ".mp4", ".mov", ".mkv"}


def _new_id() -> str:
    return "va_" + uuid.uuid4().hex[:12]


def path_of(a: dict) -> Path:
    return store.sub("assets") / a["file"]


def get(asset_id: str) -> Optional[dict]:
    with store.connect() as con:
        return store.row(con.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone(), ("meta",))


def listing(limit: int = 50, offset: int = 0) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM assets ORDER BY created DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return [store.row(r, ("meta",)) for r in rows]   # type: ignore[misc]


def lineage(asset_id: str) -> list[dict]:
    """The asset and its ancestors, newest first (stops at 50, or at a loop)."""
    out, seen = [], set()
    cur = get(asset_id)
    while cur and cur["id"] not in seen and len(out) < 50:
        out.append(cur)
        seen.add(cur["id"])
        cur = get(cur["parent_id"]) if cur.get("parent_id") else None
    return out


def children(asset_id: str) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM assets WHERE parent_id=? ORDER BY created", (asset_id,)).fetchall()
    return [store.row(r, ("meta",)) for r in rows]   # type: ignore[misc]


def _label_frames(src: Path, dst: Path, text_id: str) -> None:
    """Burn the visible AI label into every frame (OpenCV, so it works without ffmpeg; audio is carried over by ffmpeg
    when there is any and ffmpeg is present)."""
    import cv2

    from bot.likeness import provenance
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise media.MediaError(f"{src.name}: not a video OpenCV can read")
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    tmp = dst.with_name(dst.stem + ".label.webm")
    wr = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"VP90"), fps, (w, h))
    try:
        if not wr.isOpened():
            raise media.MediaError("could not open the WebM writer")
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            wr.write(provenance.label(frame, text_id))
    finally:
        cap.release()
        wr.release()
    info = None
    try:
        info = media.probe(src)
    except media.MediaError:
        pass
    if info and info.get("audio") and media.ffmpeg_path():
        media.mux_audio(tmp, src, dst)
        tmp.unlink(missing_ok=True)
    else:
        tmp.replace(dst)


def manifest(*, asset_id: str, job: dict | None, workflow: str, model: str, prompt: str, seed: Any, backend: str,
             output: Path, content_sha256: str, parent: dict | None, labelled: bool, action: str = "c2pa.created") -> dict:
    from bot.likeness import provenance
    ingredients = []
    if parent:
        ingredients.append({"title": parent["file"], "relationship": "parentOf", "asset_id": parent["id"],
                            "sha256": parent["sha256"]})
    return {
        "format": "abp-video-provenance/1 (C2PA-shaped, unsigned)",
        "claim_generator": "AgenticBotPlatform Video Studio",
        "title": output.name,
        "digital_source_type": provenance.IPTC_SOURCE,
        "ai_generated": True,
        "visible_label": provenance.LABEL if labelled else None,
        "actions": [{"action": action, "digitalSourceType": provenance.IPTC_SOURCE,
                     "softwareAgent": f"ABP video {backend} {workflow}",
                     "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}],
        "ingredients": ingredients,
        "video": {"asset_id": asset_id, "job_id": (job or {}).get("id"), "workflow": workflow, "model": model,
                  "backend": backend, "prompt": prompt, "seed": seed, "parent_id": parent["id"] if parent else None},
        "content_sha256": content_sha256,
        "signature": None,
    }


def add(src: Path, *, job: dict | None = None, workflow: str = "", model: str = "", prompt: str = "",
        seed: Any = None, backend: str = "", parent_id: str | None = None, meta: dict | None = None,
        action: str = "c2pa.created", label: bool | None = None) -> dict:
    """Move a finished file into the library with its label, provenance and thumbnail; returns the asset row."""
    from bot.likeness import provenance
    from bot.video import jobs
    src = Path(src)
    if src.suffix.lower() not in VIDEO_EXT:
        raise ValueError(f"unsupported video type {src.suffix}")
    parent = get(parent_id) if parent_id else None
    if parent_id and not parent:
        raise ValueError(f"no parent asset {parent_id}")
    aid = _new_id()
    dst = store.sub("assets") / f"{aid}{src.suffix.lower()}"
    labelled = jobs.settings().get("visible_label", True) if label is None else label
    if labelled and src.suffix.lower() == ".webm":
        _label_frames(src, dst, aid)
    else:
        labelled = False
        shutil.copyfile(src, dst)
    content = store.sha256_file(dst)                  # before the metadata is appended (as likeness does)
    m = manifest(asset_id=aid, job=job, workflow=workflow, model=model, prompt=prompt, seed=seed, backend=backend,
                 output=dst, content_sha256=content, parent=parent, labelled=bool(labelled), action=action)
    if dst.suffix == ".webm":
        provenance.append_webm(dst, m)
    side = provenance.write_sidecar(dst, m)
    info: dict = {}
    try:
        info = media.probe(dst)
    except media.MediaError as exc:
        info = {"probe_error": str(exc)}
    try:
        media.thumbnail(dst, dst.with_suffix(".jpg"))
        info["thumbnail"] = dst.with_suffix(".jpg").name
    except Exception as exc:  # noqa: BLE001 - a clip without a thumbnail is still a clip
        info["thumbnail_error"] = str(exc)
    row = {"id": aid, "kind": "video", "file": dst.name, "sha256": store.sha256_file(dst), "bytes": dst.stat().st_size,
           "job_id": (job or {}).get("id"), "parent_id": parent_id, "workflow": workflow, "model": model,
           "prompt": prompt, "seed": seed, "meta": json.dumps({**(meta or {}), **info, "labelled": bool(labelled),
                                                               "content_sha256": content}),
           "provenance": side.name, "created": store.now()}
    with store.connect() as con:
        con.execute(f"INSERT INTO assets ({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
    return get(aid)  # type: ignore[return-value]


def provenance_of(asset_id: str) -> dict:
    """The sidecar manifest and the embedded one (they must agree), plus the asset's lineage."""
    from bot.likeness import provenance
    a = get(asset_id)
    if not a:
        raise ValueError(f"no asset {asset_id}")
    p = path_of(a)
    side = p.with_name(p.name + ".provenance.json")
    return {"sidecar": json.loads(side.read_text(encoding="utf-8")) if side.is_file() else None,
            "embedded": provenance.read_embedded(p) if p.is_file() else None,
            "lineage": [{"id": x["id"], "workflow": x["workflow"], "sha256": x["sha256"]} for x in lineage(asset_id)]}


DERIVE_OPS = ("trim", "transcode", "concat")


def derive(asset_id: str, op: str, args: dict | None = None) -> dict:
    """A child asset made by the media layer from this one (lineage: parent = this asset)."""
    args = dict(args or {})
    a = get(asset_id)
    if not a:
        raise ValueError(f"no asset {asset_id}")
    if op not in DERIVE_OPS:
        raise ValueError(f"op must be one of {', '.join(DERIVE_OPS)}")
    src = path_of(a)
    work = store.sub("work") / ("derive_" + uuid.uuid4().hex[:8])
    work.mkdir(parents=True, exist_ok=True)
    try:
        if op == "trim":
            out = media.trim(src, work / "out.webm", start=float(args.get("start", 0)), end=float(args.get("end", 0)))
        elif op == "transcode":
            fmt = str(args.get("format") or "mp4").lower()
            if fmt not in ("mp4", "webm"):
                raise ValueError("format must be mp4 or webm")
            out = media.transcode(src, work / f"out.{fmt}", width=int(args.get("width") or 0))
        else:
            others = [get(str(x)) for x in (args.get("with") or [])]
            if not others or any(o is None for o in others):
                raise ValueError("concat needs `with`: a list of existing asset ids")
            out = media.concat([src, *[path_of(o) for o in others if o]], work / "out.webm")
        return add(out, workflow=f"media.{op}", model=a["model"], prompt=a["prompt"], seed=a["seed"], backend="ffmpeg",
                   parent_id=asset_id, meta={"op": op, "args": args}, action="c2pa.edited", label=False)
    finally:
        shutil.rmtree(work, ignore_errors=True)

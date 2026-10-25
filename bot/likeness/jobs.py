"""Likeness jobs: consent records from reference images, face swap and recast jobs, character profiles and their LoRA
datasets. The API and the agent tools both come through here, so the rules (policy.py) apply to every caller.

Jobs run one at a time on a worker thread; each start, refusal, finish and failure is written to the job log
(store.log_job) and to ABP's audit log."""
from __future__ import annotations

import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

from bot.likeness import engine, policy, provenance, store

DEFAULTS = {"enabled": False, "max_seconds": 60}
MODES = ("faceswap", "recast")
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
UPLOAD_MAX = 512 << 20

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_run_lock = threading.Lock()


def settings() -> dict:
    out = dict(DEFAULTS)
    try:
        from bot.config import config
        got = config.current.get("likeness") or {}
    except Exception:  # noqa: BLE001
        got = {}
    if isinstance(got, dict):
        out.update({k: got[k] for k in DEFAULTS if k in got})
    return out


def _audit(action: str, detail: str) -> None:
    try:
        from bot import db
        db.log_audit(actor="likeness", action=action, detail=detail[:1000])
    except Exception:  # noqa: BLE001 - the job log still has it
        pass


# ---- inputs ------------------------------------------------------------------------------------------------------
def save_upload(name: str, data: bytes) -> dict:
    ext = Path(name or "").suffix.lower()
    if ext not in VIDEO_EXT | IMAGE_EXT:
        raise ValueError(f"unsupported file type {ext or '(none)'}")
    if len(data) > UPLOAD_MAX:
        raise ValueError(f"uploads are limited to {UPLOAD_MAX >> 20} MB")
    if policy.text_problem([name]):
        raise policy.Refused(policy.text_problem([name]))
    uid = uuid.uuid4().hex[:12]
    p = store.sub("uploads") / (uid + ext)
    p.write_bytes(data)
    return {"id": f"upload:{uid}{ext}", "name": name, "bytes": len(data), "kind": "video" if ext in VIDEO_EXT else "image"}


def resolve(src: str) -> Path:
    """An input reference: "upload:<id>" (from save_upload) or a file path on this machine."""
    if not isinstance(src, str) or not src.strip():
        raise ValueError("an input file is required")
    s = src.strip()
    if s.startswith("upload:"):
        name = s[7:]
        if not re.fullmatch(r"[0-9a-f]{12}\.[a-z0-9]{2,4}", name):
            raise ValueError("bad upload id")
        p = store.sub("uploads") / name
    else:
        p = Path(s).expanduser()
    if not p.is_file():
        raise ValueError(f"{p.name}: no such file")
    return p


def _image(src: str) -> tuple[np.ndarray, str]:
    if src.startswith("data:"):
        from bot.vision import images
        img, _ = images.load(src)
        ok, buf = cv2.imencode(".png", img)
        return img, provenance.sha256_bytes(buf.tobytes())
    p = resolve(src)
    img = cv2.imread(str(p), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"{p.name}: not an image OpenCV can read")
    return img, store.sha256_file(p)


def _one_face(img: np.ndarray, what: str) -> dict:
    fs = engine.faces(img)
    if not fs:
        raise ValueError(f"no face found in the {what}")
    return engine.largest(fs)


# ---- consent -----------------------------------------------------------------------------------------------------
def create_consent(body: dict) -> dict:
    refs = body.get("images") or []
    if isinstance(refs, str):
        refs = [refs]
    if not refs or len(refs) > 8:
        raise ValueError("give 1 to 8 reference images of the person")
    why = policy.text_problem([body.get("name"), body.get("scope"), body.get("statement")])
    if why:
        _audit("likeness_consent_refused", why)
        raise policy.Refused(why)
    faces, shas = [], []
    for r in refs:
        img, sha = _image(str(r))
        faces.append({"sface": engine.sface_feature(img, _one_face(img, "reference image")), "image_sha256": sha})
        shas.append(sha)
    c = store.add_consent(name=str(body.get("name") or ""), kind=str(body.get("kind") or ""),
                          attested_by=str(body.get("attested_by") or ""), adult=body.get("adult") is True,
                          statement=str(body.get("statement") or ""), scope=str(body.get("scope") or ""),
                          faces=faces, image_sha256=shas)
    _audit("likeness_consent_added", f"{c['id']} {c['name']} ({c['kind']}) attested by {c['attested_by']}")
    store.log_job({"event": "consent_added", "consent_id": c["id"], "name": c["name"], "kind": c["kind"]})
    return c


def revoke_consent(cid: str, reason: str = "") -> Optional[dict]:
    c = store.revoke_consent(cid, reason)
    if c:
        _audit("likeness_consent_revoked", f"{cid}: {reason}")
        store.log_job({"event": "consent_revoked", "consent_id": cid, "reason": reason})
    return c


def _match_consent(consent: dict, img: np.ndarray, face: dict) -> float:
    feat = engine.sface_feature(img, face)
    return max(engine.cosine(feat, f["sface"]) for f in consent["faces"])


# ---- jobs --------------------------------------------------------------------------------------------------------
def _refuse(job_id: str, why: str, body: dict) -> None:
    store.log_job({"event": "refused", "job_id": job_id, "mode": body.get("mode"), "consent_id": body.get("consent_id"),
                   "reason": why})
    _audit("likeness_refused", f"{job_id}: {why}")


def submit(body: dict) -> dict:
    """Check a face swap / recast request against the rules and queue it. Raises policy.Refused or ValueError."""
    job_id = "lk_" + uuid.uuid4().hex[:10]
    mode = body.get("mode")
    try:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        consent = store.consent(str(body.get("consent_id") or ""), raw=True)
        policy.check(consent, [body.get("purpose"), body.get("source"), body.get("target"), body.get("note")],
                     enabled=bool(settings()["enabled"]))
        if not body.get("purpose") or len(str(body["purpose"]).strip()) < 3:
            raise ValueError("say what the output is for (purpose)")
        target = str(body.get("target_face") or ("best" if body.get("target_reference") else "largest"))
        if target not in engine.TARGETS:
            raise ValueError(f"target_face must be one of {', '.join(engine.TARGETS)}")
        if target == "best" and not body.get("target_reference"):
            raise ValueError("target_face 'best' needs a target_reference image of the person to replace")
        tgt = resolve(str(body.get("target") or ""))
        kind = "video" if tgt.suffix.lower() in VIDEO_EXT else "image"
        if mode == "recast" and kind != "video":
            raise ValueError("recast works on a video; use faceswap for an image")
    except policy.Refused as e:
        _refuse(job_id, str(e), body)
        raise
    job = {"id": job_id, "mode": mode, "kind": kind, "status": "queued", "consent_id": consent["id"],
           "person": consent["name"], "purpose": str(body["purpose"]).strip()[:500], "target_face": target,
           "created": time.time(), "progress": 0.0, "output": None, "error": None}
    with _jobs_lock:
        _jobs[job_id] = job
    store.log_job({"event": "queued", **{k: job[k] for k in ("id", "mode", "kind", "consent_id", "person", "purpose")}})
    _audit("likeness_job_queued", f"{job_id} {mode} {kind} under {consent['id']} ({consent['name']}): {job['purpose']}")
    threading.Thread(target=_run, args=(job, consent, dict(body), tgt), daemon=True, name=f"likeness-{job_id}").start()
    return dict(job)


def _run(job: dict, consent: dict, body: dict, tgt: Path) -> None:
    with _run_lock:
        job.update(status="running", started=time.time())
        try:
            _do(job, consent, body, tgt)
            job.update(status="done", progress=1.0, finished=time.time())
            store.log_job({"event": "done", "job_id": job["id"], "output": job["output"], "stats": job.get("stats")})
            _audit("likeness_job_done", f"{job['id']} -> {job['output']}")
        except policy.Refused as e:
            job.update(status="refused", error=str(e), finished=time.time())
            _refuse(job["id"], str(e), body)
        except Exception as e:  # noqa: BLE001 - the job records it
            job.update(status="failed", error=f"{type(e).__name__}: {e}", finished=time.time())
            store.log_job({"event": "failed", "job_id": job["id"], "error": job["error"]})
            _audit("likeness_job_failed", f"{job['id']}: {job['error']}")


def _do(job: dict, consent: dict, body: dict, tgt: Path) -> None:
    src_img, src_sha = _image(str(body.get("source") or ""))
    src_face = _one_face(src_img, "source image")
    sim = _match_consent(consent, src_img, src_face)
    if sim < engine.MATCH_COSINE:
        raise policy.Refused(f"the source face does not match {consent['name']}'s consent reference "
                             f"(similarity {sim:.2f} < {engine.MATCH_COSINE}); add a consent record for this person")
    latent = engine.source_latent(src_img, src_face)
    ref_feat = None
    if body.get("target_reference"):
        r_img, _ = _image(str(body["target_reference"]))
        ref_feat = engine.sface_feature(r_img, _one_face(r_img, "target reference"))
    out_dir = store.sub("out")
    inputs = [{"role": "source", "sha256": src_sha, "consent_id": consent["id"]},
              {"role": "target", "name": tgt.name, "sha256": store.sha256_file(tgt)}]
    stats: dict[str, Any] = {"source_match": round(sim, 3)}
    if job["kind"] == "image":
        img = cv2.imread(str(tgt), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError(f"{tgt.name}: not an image OpenCV can read")
        out, n = engine.swap_image(img, latent, job["target_face"], ref_feat)
        if not n:
            raise ValueError("no matching face in the target image")
        out = provenance.label(out, job["id"])
        path = out_dir / f"{job['id']}.png"
        ok, buf = cv2.imencode(".png", out)
        m = provenance.manifest(job_id=job["id"], mode=job["mode"], consent=consent, inputs=inputs, output=path,
                                content_sha256=provenance.sha256_bytes(buf.tobytes()), extra={"purpose": job["purpose"]})
        provenance.write_png(out, path, m)
        stats["faces_swapped"] = n
    else:
        path = out_dir / f"{job['id']}.webm"
        st = engine.swap_video(tgt, path, latent, target=job["target_face"], ref_feature=ref_feat,
                               max_seconds=float(settings()["max_seconds"]),
                               frame_hook=lambda f: provenance.label(f, job["id"]),
                               progress=lambda d, t: job.update(progress=round(d / max(t, 1), 3)))
        if not st["frames_swapped"]:
            path.unlink(missing_ok=True)
            raise ValueError("no matching face in any frame of the target video")
        stats.update(st)
        m = provenance.manifest(job_id=job["id"], mode=job["mode"], consent=consent, inputs=inputs, output=path,
                                content_sha256=store.sha256_file(path), extra={"purpose": job["purpose"], "video": st})
        provenance.append_webm(path, m)
    provenance.write_sidecar(path, m)
    job.update(output=path.name, stats=stats)


def jobs() -> list[dict]:
    with _jobs_lock:
        return sorted((dict(j) for j in _jobs.values()), key=lambda j: -j["created"])


def job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


# ---- characters --------------------------------------------------------------------------------------------------
def create_character(body: dict) -> dict:
    """A named character for a consented person: their reference set, for reference-image conditioning and as the
    input of a per-person LoRA dataset."""
    consent = store.consent(str(body.get("consent_id") or ""), raw=True)
    name = str(body.get("name") or "").strip()
    policy.check(consent, [name, body.get("trigger")], enabled=bool(settings()["enabled"]))
    if not name:
        raise ValueError("name is required")
    trigger = re.sub(r"[^a-z0-9]", "", str(body.get("trigger") or name).lower())[:24] or "person"
    refs = body.get("images") or []
    if not refs or len(refs) > 40:
        raise ValueError("give 1 to 40 reference images")
    kept = []
    d = store.sub("characters")
    for r in refs:
        img, sha = _image(str(r))
        f = _one_face(img, "reference image")
        if _match_consent(consent, img, f) < engine.MATCH_COSINE:
            raise policy.Refused(f"a reference image does not show {consent['name']} (consent {consent['id']})")
        p = d / f"{sha[:16]}.png"
        cv2.imwrite(str(p), img)
        kept.append(p.name)
    ch = store.add_character(name=name, consent_id=consent["id"], trigger="abp" + trigger, images=kept)
    _audit("likeness_character_added", f"{ch['id']} {name} under {consent['id']}")
    store.log_job({"event": "character_added", "character_id": ch["id"], "consent_id": consent["id"]})
    return ch


def build_dataset(chid: str, size: int = 512) -> dict:
    """A LoRA training set for the character: face-centred square crops with captions using its trigger word, in
    data/likeness/datasets/<id> (kohya / sd-scripts layout: image + .txt). Training itself runs later in the V1
    engine; this is its input."""
    ch = store.character(chid)
    if not ch:
        raise ValueError("no such character")
    consent = store.consent(ch["consent_id"], raw=True)
    policy.check(consent, [ch["name"]], enabled=bool(settings()["enabled"]))
    out = store.sub("datasets") / chid / f"10_{ch['trigger']}"
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for name in ch["images"]:
        img = cv2.imread(str(store.sub("characters") / name), cv2.IMREAD_COLOR)
        if img is None:
            continue
        f = _one_face(img, "reference image")
        x, y, w, h = f["box"]
        cx, cy, side = x + w / 2, y + h / 2, int(max(w, h) * 2.2)
        x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        crop = img[y0:y0 + side, x0:x0 + side]
        if crop.size == 0:
            continue
        crop = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(out / f"{n:03d}.png"), crop)
        (out / f"{n:03d}.txt").write_text(f"photo of {ch['trigger']} person", encoding="utf-8")
        n += 1
    info = {"path": str(out.parent), "images": n, "size": size, "built": time.time()}
    store.update_character(chid, dataset=info)
    _audit("likeness_dataset_built", f"{chid}: {n} images")
    store.log_job({"event": "dataset_built", "character_id": chid, "images": n})
    return info


def output_path(name: str) -> Optional[Path]:
    if not re.fullmatch(r"lk_[0-9a-f]{10}\.(png|webm)(\.provenance\.json)?", name or ""):
        return None
    p = store.sub("out") / name
    return p if p.is_file() else None

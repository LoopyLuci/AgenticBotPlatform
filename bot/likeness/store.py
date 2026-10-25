"""Consent records, character profiles and the likeness job log, kept in data/likeness ($ABP_LIKENESS_DIR overrides).

A consent record is never deleted from here, only revoked: the audit trail must say under which consent every past
output was made. Reference faces are kept as SFace features plus the SHA-256 of the image they came from."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

_lock = threading.Lock()
KINDS = ("self", "consented")


def root() -> Path:
    over = os.environ.get("ABP_LIKENESS_DIR")
    if over:
        d = Path(over)
    else:
        from bot import envfile
        d = Path(envfile.PROJECT_ROOT) / "data" / "likeness"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sub(name: str) -> Path:
    d = root() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(name: str) -> list[dict]:
    p = root() / name
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write(name: str, rows: list[dict]) -> None:
    p = root() / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _public(c: dict) -> dict:
    return {k: v for k, v in c.items() if k != "faces"} | {"reference_faces": len(c.get("faces") or [])}


# ---- consent ---------------------------------------------------------------------------------------------------
def consents(include_revoked: bool = True) -> list[dict]:
    with _lock:
        return [_public(c) for c in _read("consents.json") if include_revoked or not c.get("revoked")]


def consent(cid: str, *, raw: bool = False) -> Optional[dict]:
    with _lock:
        for c in _read("consents.json"):
            if c.get("id") == cid:
                return c if raw else _public(c)
    return None


def add_consent(*, name: str, kind: str, attested_by: str, adult: bool, statement: str, scope: str = "",
                faces: list[dict], image_sha256: list[str]) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not name.strip() or not attested_by.strip():
        raise ValueError("name and attested_by are required")
    if not statement.strip():
        raise ValueError("the attestation statement is required")
    if adult is not True:
        raise ValueError("the person must be attested as an adult (18+)")
    if not faces:
        raise ValueError("at least one reference image with one clear face is required")
    c = {"id": "c_" + uuid.uuid4().hex[:10], "name": name.strip()[:120], "kind": kind, "adult": True,
         "attested_by": attested_by.strip()[:120], "statement": statement.strip()[:2000], "scope": scope.strip()[:500],
         "created": time.time(), "revoked": None, "image_sha256": image_sha256, "faces": faces}
    with _lock:
        rows = _read("consents.json")
        rows.append(c)
        _write("consents.json", rows)
    return _public(c)


def revoke_consent(cid: str, reason: str = "") -> Optional[dict]:
    with _lock:
        rows = _read("consents.json")
        for c in rows:
            if c.get("id") == cid:
                if not c.get("revoked"):
                    c["revoked"] = {"at": time.time(), "reason": reason.strip()[:500]}
                    _write("consents.json", rows)
                return _public(c)
    return None


# ---- characters (a consented person's reference set, for reference-image generation and LoRA datasets) ---------
def characters() -> list[dict]:
    with _lock:
        return _read("characters.json")


def character(chid: str) -> Optional[dict]:
    return next((c for c in characters() if c.get("id") == chid), None)


def add_character(*, name: str, consent_id: str, trigger: str, images: list[str]) -> dict:
    ch = {"id": "ch_" + uuid.uuid4().hex[:10], "name": name.strip()[:120], "consent_id": consent_id,
          "trigger": trigger, "images": images, "created": time.time(), "dataset": None}
    with _lock:
        rows = _read("characters.json")
        rows.append(ch)
        _write("characters.json", rows)
    return ch


def update_character(chid: str, **fields: Any) -> Optional[dict]:
    with _lock:
        rows = _read("characters.json")
        for c in rows:
            if c.get("id") == chid:
                c.update(fields)
                _write("characters.json", rows)
                return c
    return None


# ---- the job log (append-only) ---------------------------------------------------------------------------------
def log_job(entry: dict) -> None:
    line = json.dumps({"at": time.time(), **entry}, default=str)
    with _lock, open(root() / "jobs.jsonl", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def job_log(limit: int = 50) -> list[dict]:
    p = root() / "jobs.jsonl"
    if not p.is_file():
        return []
    with _lock:
        lines = p.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 1000)):]
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

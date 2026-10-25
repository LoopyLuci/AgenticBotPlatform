"""The model files likeness uses, pinned by size and SHA-256 and fetched on first use (likeness.models_dir, else
data/likeness/models).

- inswapper_128 (fp16): InsightFace's face swapper. License: InsightFace's models are for non-commercial research
  use; ABP uses it on the owner's own machine only. Mirror: the FaceFusion assets release (models-3.0.0).
- arcface_w600k_r50: InsightFace's ArcFace recogniser (from buffalo_l), whose embedding drives the swapper. Same
  license and mirror.

The swapper is patched once after download (onnxfix) so OpenCV's DNN can run it; the patched copy and its emap sit
next to the original and are rebuilt if missing."""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from bot.likeness import onnxfix, store

BASE = "https://github.com/facefusion/facefusion-assets/releases/download/models-3.0.0/"
_lock = threading.Lock()
_checked: set[Path] = set()


@dataclass(frozen=True)
class File:
    key: str
    name: str
    size: int
    sha256: str
    license: str

    @property
    def url(self) -> str:
        return BASE + self.name


FILES = {f.key: f for f in (
    File("swapper", "inswapper_128_fp16.onnx", 277680829,
         "c4eccca86ad177586c85c28bf1a64a9d9ed237e283a15818d831f7facfd3f420", "InsightFace (non-commercial research)"),
    File("arcface", "arcface_w600k_r50.onnx", 174388474,
         "f1f79dc3b0b79a69f94799af1fffebff09fbd78fd96a275fd8f0cbbea23270d1", "InsightFace (non-commercial research)"),
)}


def models_dir() -> Path:
    over = os.environ.get("ABP_LIKENESS_MODELS_DIR")
    if not over:
        try:
            from bot.config import config
            over = ((config.current.get("likeness") or {}).get("models_dir") or "")
        except Exception:  # noqa: BLE001 - the default folder works without config
            over = ""
    d = Path(over) if over else store.sub("models")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verified(p: Path, f: File) -> bool:
    return p.is_file() and p.stat().st_size == f.size and _sha(p) == f.sha256


def _fetch(f: File) -> Path:
    dest = models_dir() / f.name
    if dest in _checked:
        return dest                     # hashed once per process; re-hashing 450 MB on every job is wasteful
    if _verified(dest, f):
        _checked.add(dest)
        return dest
    # missing, or damaged (a hard crash once left a right-sized file of zeros): fetch again, drop what was made from it
    for derived in ("inswapper_128_opencv.onnx", "inswapper_128_emap.npy"):
        (dest.parent / derived).unlink(missing_ok=True)
    tmp = dest.with_suffix(".part")
    req = urllib.request.Request(f.url, headers={"User-Agent": "AgenticBotPlatform-likeness/1"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as out:
        shutil.copyfileobj(r, out, 1 << 20)
    if not _verified(tmp, f):
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{f.name}: the download does not match its pinned SHA-256; not used")
    os.replace(tmp, dest)
    _checked.add(dest)
    return dest


def _derived_ok(cv: Path, emap: Path) -> bool:
    """The patched swapper and its emap look whole (a crash can leave zero-filled files of the right size)."""
    try:
        if not (cv.is_file() and cv.stat().st_size > FILES["swapper"].size // 2):
            return False
        with open(cv, "rb") as fh:
            if fh.read(64).count(0) == 64:
                return False
        e = np.load(emap)
        return e.shape == (512, 512) and bool(np.isfinite(e).all()) and bool(e.any())
    except (OSError, ValueError):
        return False


def ensure() -> dict[str, Path]:
    """{"swapper": the OpenCV-ready swapper, "emap": its .npy, "arcface": the recogniser}, fetched if needed."""
    with _lock:
        sw, arc = _fetch(FILES["swapper"]), _fetch(FILES["arcface"])
        cv, emap = sw.with_name("inswapper_128_opencv.onnx"), sw.with_name("inswapper_128_emap.npy")
        if not _derived_ok(cv, emap):
            data, em, _ = onnxfix.patch_inswapper(sw.read_bytes())
            cv.with_suffix(".part").write_bytes(data)
            os.replace(cv.with_suffix(".part"), cv)
            np.save(emap, em)
        return {"swapper": cv, "emap": emap, "arcface": arc}


def status() -> list[dict]:
    d = models_dir()
    return [{"key": f.key, "name": f.name, "size": f.size, "license": f.license,
             "present": (d / f.name).is_file() and (d / f.name).stat().st_size == f.size} for f in FILES.values()]

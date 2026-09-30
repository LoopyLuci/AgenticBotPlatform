"""OpenCV's model zoo (github.com/opencv/opencv_zoo, Apache-2.0 code; each model has its own license, listed below):
the models ABP's vision uses, fetched on first use into data/vision/models and verified against their SHA-256
(the zoo's Git LFS object ids). When the opencv-zoo module's checkout has a model's real file (after its
models.fetch), it is copied from there instead of downloaded."""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bot.vision.images import VisionError, vision_dir

LFS = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
RAW = "https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/"


@dataclass(frozen=True)
class File:
    dir: str
    name: str
    size: int
    sha256: str
    lfs: bool = True

    @property
    def url(self) -> str:
        return (LFS if self.lfs else RAW) + f"{self.dir}/{self.name}"


@dataclass(frozen=True)
class Model:
    key: str
    title: str
    files: tuple[File, ...]
    license: str
    task: str

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)


MODELS: dict[str, Model] = {m.key: m for m in (
    Model("face_detect", "YuNet face detection (2023mar)", (
        File("face_detection_yunet", "face_detection_yunet_2023mar.onnx", 232589,
             "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),), "MIT", "faces"),
    Model("face_recognize", "SFace face recognition (2021dec)", (
        File("face_recognition_sface", "face_recognition_sface_2021dec.onnx", 38696353,
             "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),), "Apache-2.0", "faces"),
    Model("objects", "YOLOX-S object detection, COCO's 80 classes (2022nov)", (
        File("object_detection_yolox", "object_detection_yolox_2022nov.onnx", 35858002,
             "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"),), "Apache-2.0", "objects"),
    Model("text_detect", "PP-OCRv3 text detection (2023may)", (
        File("text_detection_ppocr", "text_detection_en_ppocrv3_2023may.onnx", 2423490,
             "03f550c6b406fda8bf54bd8327815f6c7e2edd98cea02348c93d879254366587"),), "Apache-2.0", "text"),
    Model("text_recognize", "CRNN text recognition: digits, letters and punctuation (2023feb, fp16)", (
        File("text_recognition_crnn", "text_recognition_CRNN_CH_2023feb_fp16.onnx", 32472394,
             "cfef028889b3a21771e687d501ac38ccab6d37d199e94f244d60cc21f743526b"),), "Apache-2.0", "text"),
    Model("people", "PP-HumanSeg human segmentation (2023mar)", (
        File("human_segmentation_pphumanseg", "human_segmentation_pphumanseg_2023mar.onnx", 6163938,
             "552d8a984054e59b5d773d24b9b12022b22046ceb2bbc4c9aaeaceb36a9ddf24"),), "Apache-2.0", "people"),
    Model("track", "VitTrack object tracking (2023sep)", (
        File("object_tracking_vittrack", "object_tracking_vittrack_2023sep.onnx", 714726,
             "2990f0b7cd44d92afa48cd97db6de7be113fc1d9594fddb74e2725c10478e91d"),), "Apache-2.0", "tracking"),
)}

_lock = threading.Lock()


def models_dir() -> Path:
    """data/vision/models ($ABP_VISION_MODELS_DIR overrides it: tests share one verified cache)."""
    over = os.environ.get("ABP_VISION_MODELS_DIR")
    d = Path(over) if over else vision_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(f: File) -> Path:
    return models_dir() / f.dir / f.name


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_zoo() -> Optional[Path]:
    """The opencv-zoo module's checkout, if ABP knows one."""
    over = os.environ.get("ABP_OPENCV_ZOO_DIR")
    if over:
        return Path(over)
    try:
        from bot.modules import registry
        m = registry.modules().get("opencv-zoo")
        d = registry.install_dir(m) if m else None
        return d if d and (d / "models").is_dir() else None
    except Exception:  # noqa: BLE001 - vision works without the module
        return None


def _verified(p: Path, f: File) -> bool:
    return p.is_file() and p.stat().st_size == f.size and _sha256(p) == f.sha256


def _fetch(f: File) -> str:
    dest = _path(f)
    if _verified(dest, f):
        return "present"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    zoo = _local_zoo()
    src = zoo / "models" / f.dir / f.name if zoo else None
    if src is not None and src.is_file():
        data = src.read_bytes()
        if not f.lfs:
            data = data.replace(b"\r\n", b"\n")        # a checkout may have converted the text files' line endings
        if len(data) == f.size and hashlib.sha256(data).hexdigest() == f.sha256:
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            return "copied from the opencv-zoo checkout"
    req = urllib.request.Request(f.url, headers={"User-Agent": "AgenticBotPlatform-vision/1"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as out:
        shutil.copyfileobj(r, out, 1 << 20)
    if not _verified(tmp, f):
        got = tmp.stat().st_size
        tmp.unlink(missing_ok=True)
        raise VisionError(f"{f.name}: the download does not match its SHA-256 ({got} bytes); not used")
    os.replace(tmp, dest)
    return "downloaded"


def ensure(key: str) -> list[Path]:
    """The model's files, fetched and verified if they are not here yet."""
    m = MODELS.get(key)
    if m is None:
        raise VisionError(f"unknown model {key!r} (known: {', '.join(MODELS)})")
    with _lock:
        for f in m.files:
            _fetch(f)
    return [_path(f) for f in m.files]


def fetch(key: str) -> dict:
    m = MODELS.get(key)
    if m is None:
        raise VisionError(f"unknown model {key!r} (known: {', '.join(MODELS)})")
    with _lock:
        how = {f.name: _fetch(f) for f in m.files}
    return {"model": key, "files": how}


def status() -> list[dict]:
    out = []
    for m in MODELS.values():
        present = all(_path(f).is_file() and _path(f).stat().st_size == f.size for f in m.files)
        out.append({"model": m.key, "title": m.title, "task": m.task, "license": m.license,
                    "size_mb": round(m.size / 1e6, 1), "present": present})
    return out

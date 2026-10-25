"""Face swap on this machine: YuNet finds faces (bot.vision), ArcFace embeds the source face, InsightFace's inswapper
makes the swapped 128x128 face, which is warped back and blended in with a feathered mask. All on OpenCV's DNN
(CPU; no new dependency, no driver requirement).

Recast = the same swap on every frame of a video, applied to one chosen person: the face that best matches a
reference image of the person to replace (SFace), else the largest face. "all" swaps every face found."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from bot.likeness import models

ARCFACE_112 = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                        [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)
MATCH_COSINE = 0.363                     # SFace: the zoo's "same person" threshold
TARGETS = ("best", "largest", "all")

_lock = threading.Lock()
_nets: dict = {}


class LikenessError(Exception):
    pass


def _net(key: str):
    with _lock:
        if key not in _nets:
            paths = models.ensure()
            if key == "emap":
                _nets[key] = np.load(paths["emap"])
            else:
                _nets[key] = cv2.dnn.readNet(str(paths[key]))
        return _nets[key]


# ---- faces -------------------------------------------------------------------------------------------------------
def faces(img: np.ndarray, min_score: float = 0.6) -> list[dict]:
    from bot.vision import pipelines
    return pipelines.faces(img, min_score=min_score)


def landmarks(face: dict) -> np.ndarray:
    """The five points in the ArcFace template's order (eyes and mouth corners left-to-right in the image)."""
    lm = face["landmarks"]
    eyes = sorted([lm["right_eye"], lm["left_eye"]], key=lambda p: p[0])
    mouth = sorted([lm["mouth_right"], lm["mouth_left"]], key=lambda p: p[0])
    return np.array([eyes[0], eyes[1], lm["nose"], mouth[0], mouth[1]], dtype=np.float32)


def _align(img: np.ndarray, pts: np.ndarray, size: int) -> tuple[np.ndarray, np.ndarray]:
    dst = ARCFACE_112 * (size / 112.0)
    m, _ = cv2.estimateAffinePartial2D(pts, dst, method=cv2.LMEDS)
    if m is None:
        raise LikenessError("could not align the face")
    return cv2.warpAffine(img, m, (size, size), borderValue=0.0), m


def sface_feature(img: np.ndarray, face: dict) -> list[float]:
    from bot.vision import pipelines
    return [float(x) for x in np.asarray(pipelines.face_features(img, face)).ravel()]


def cosine(a, b) -> float:
    a, b = np.asarray(a, np.float32).ravel(), np.asarray(b, np.float32).ravel()
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def largest(fs: list[dict]) -> Optional[dict]:
    return max(fs, key=lambda f: f["box"][2] * f["box"][3]) if fs else None


def source_latent(img: np.ndarray, face: dict) -> np.ndarray:
    """The swapper's source input: ArcFace embedding (normalised) through the model's emap, normalised."""
    crop, _ = _align(img, landmarks(face), 112)
    net = _net("arcface")
    net.setInput(cv2.dnn.blobFromImage(crop, 1.0 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True))
    emb = net.forward().reshape(-1)
    emb = emb / (np.linalg.norm(emb) + 1e-9)
    lat = emb.reshape(1, -1) @ _net("emap")
    return (lat / (np.linalg.norm(lat) + 1e-9)).astype(np.float32)


def swap_face(img: np.ndarray, face: dict, latent: np.ndarray) -> np.ndarray:
    """img with `face` replaced by the source identity (in place on a copy)."""
    crop, m = _align(img, landmarks(face), 128)
    net = _net("swapper")
    net.setInput(cv2.dnn.blobFromImage(crop, 1.0 / 255, (128, 128), (0, 0, 0), swapRB=True), "target")
    net.setInput(latent, "source")
    out = net.forward()[0].transpose(1, 2, 0)
    fake = np.clip(out * 255, 0, 255).astype(np.uint8)[:, :, ::-1]
    h, w = img.shape[:2]
    im = cv2.invertAffineTransform(m)
    back = cv2.warpAffine(fake, im, (w, h), borderMode=cv2.BORDER_REPLICATE)
    mask = cv2.warpAffine(np.full((128, 128), 255, np.uint8), im, (w, h))
    ys, xs = np.where(mask > 20)
    if not len(ys):
        return img.copy()
    size = int(np.sqrt((ys.max() - ys.min()) * (xs.max() - xs.min())))
    k = max(size // 10, 3)
    mask = cv2.erode(mask, np.ones((k, k), np.uint8))
    b = max(size // 20, 3) * 2 + 1
    mask = cv2.GaussianBlur(mask, (b, b), 0).astype(np.float32)[:, :, None] / 255.0
    return (mask * back + (1 - mask) * img).astype(np.uint8)


def choose(fs: list[dict], img: np.ndarray, target: str, ref_feature: Optional[list]) -> list[dict]:
    if not fs:
        return []
    if target == "all":
        return fs
    if target == "best" and ref_feature is not None:
        scored = [(cosine(sface_feature(img, f), ref_feature), f) for f in fs]
        s, f = max(scored, key=lambda t: t[0])
        return [f] if s >= MATCH_COSINE else []
    return [largest(fs)]


def swap_image(img: np.ndarray, latent: np.ndarray, target: str = "largest", ref_feature: Optional[list] = None) -> tuple[np.ndarray, int]:
    fs = faces(img)
    out = img
    chosen = choose(fs, img, target, ref_feature)
    for f in chosen:
        out = swap_face(out, f, latent)
    return out, len(chosen)


def swap_video(src: Path, dst: Path, latent: np.ndarray, *, target: str, ref_feature: Optional[list],
               max_seconds: float, frame_hook: Callable[[np.ndarray], np.ndarray],
               progress: Callable[[int, int], None] = lambda done, total: None) -> dict:
    """Swap through a video into a WebM (VP9). Audio is not carried over yet. Returns frame counts."""
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise LikenessError(f"{src.name}: OpenCV cannot read this video")
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    limit = int(fps * max_seconds)
    total = min(total, limit) if total > 0 else limit
    wr = cv2.VideoWriter(str(dst), cv2.VideoWriter_fourcc(*"VP90"), fps, (w, h))
    if not wr.isOpened():
        cap.release()
        raise LikenessError("could not open the WebM writer")
    n = swapped = 0
    try:
        while n < limit:
            ok, frame = cap.read()
            if not ok:
                break
            out, k = swap_image(frame, latent, target, ref_feature)
            swapped += bool(k)
            wr.write(frame_hook(out))
            n += 1
            if n % 5 == 0 or n == total:
                progress(n, total)
    finally:
        cap.release()
        wr.release()
    if n == 0:
        raise LikenessError(f"{src.name}: no frames could be read")
    return {"frames": n, "frames_swapped": swapped, "fps": fps, "size": [w, h], "truncated": n >= limit}

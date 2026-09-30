"""Where images come from and where results go.

A source is one of:
    a file path                  "C:/pics/a.png", "~/shot.jpg"
    an http(s) URL               fetched with a size cap, public addresses only
    a data: URL                  "data:image/png;base64,..."
    "screen" / "screen:<n>"      a screenshot of every monitor, or of monitor n (1 = the first)
    "camera" / "camera:<n>"      one frame from camera n (default 0)
"""
from __future__ import annotations

import base64
import ipaddress
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import cv2
import numpy as np

MAX_BYTES = 40 * 1024 * 1024          # a source larger than this is refused
MAX_PIXELS = 80_000_000               # ~ 10000 x 8000
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif", ".jp2", ".pbm", ".pgm", ".ppm")


class VisionError(ValueError):
    pass


def vision_dir() -> Path:
    """data/vision under ABP's state folder ($ABP_VISION_DIR overrides it)."""
    over = os.environ.get("ABP_VISION_DIR")
    if over:
        d = Path(over)
    else:
        from bot import envfile
        d = Path(envfile.PROJECT_ROOT) / "data" / "vision"
    d.mkdir(parents=True, exist_ok=True)
    return d


def out_dir() -> Path:
    d = vision_dir() / "out"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _decode(buf: bytes, what: str) -> np.ndarray:
    if len(buf) > MAX_BYTES:
        raise VisionError(f"{what} is larger than {MAX_BYTES >> 20} MB")
    img = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise VisionError(f"{what} is not an image OpenCV can read")
    return _checked(img, what)


def _checked(img: np.ndarray, what: str) -> np.ndarray:
    if img.size == 0 or img.shape[0] * img.shape[1] > MAX_PIXELS:
        raise VisionError(f"{what}: {img.shape[1]}x{img.shape[0]} is empty or too large")
    return img


def _public_host(host: str) -> None:
    """Refuse URLs that point inside this machine or its private networks (an agent must not reach the LAN)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise VisionError(f"cannot resolve {host}: {e}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise VisionError(f"{host} is a private or local address; only public URLs are fetched")


def _from_url(url: str) -> np.ndarray:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise VisionError("only http(s) URLs are fetched")
    _public_host(parts.hostname)
    req = urllib.request.Request(url, headers={"User-Agent": "AgenticBotPlatform-vision/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        buf = r.read(MAX_BYTES + 1)
    return _decode(buf, url)


def _from_data_url(src: str) -> np.ndarray:
    m = re.match(r"data:image/[\w.+-]+;base64,(.*)$", src, re.S)
    if not m:
        raise VisionError("a data: URL must be data:image/<type>;base64,...")
    try:
        buf = base64.b64decode(m.group(1), validate=False)
    except ValueError as e:
        raise VisionError(f"bad base64: {e}") from None
    return _decode(buf, "the data: URL")


def screenshot(monitor: int = 0) -> tuple[np.ndarray, dict]:
    """Every monitor (0) or monitor n (1-based). Returns the image and where it sits on the virtual desktop, so a
    position found in it can be turned into a screen position (screen_x = left + x)."""
    try:
        from PIL import ImageGrab
    except ImportError as e:  # pragma: no cover - Pillow is a dependency
        raise VisionError(f"screen capture needs Pillow: {e}") from None
    try:
        shot = ImageGrab.grab(all_screens=True)
    except OSError as e:
        raise VisionError(f"cannot capture the screen here ({e}); a desktop session is needed") from None
    img = cv2.cvtColor(np.asarray(shot), cv2.COLOR_RGB2BGR)
    left, top = 0, 0
    if os.name == "nt":
        import ctypes
        user32 = ctypes.windll.user32
        left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)   # the virtual screen's origin
    region = {"left": left, "top": top, "width": img.shape[1], "height": img.shape[0], "monitor": 0}
    if monitor > 0:
        mons = monitors()
        if monitor > len(mons):
            raise VisionError(f"monitor {monitor}: there are {len(mons)}")
        m = mons[monitor - 1]
        x, y = m["left"] - left, m["top"] - top
        img = img[y:y + m["height"], x:x + m["width"]].copy()
        region = {**m, "monitor": monitor}
    return img, region


def monitors() -> list[dict]:
    """Each monitor's position and size on the virtual desktop (Windows; elsewhere one entry for the whole screen)."""
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes as wt
        out: list[dict] = []
        proc = ctypes.WINFUNCTYPE(ctypes.c_int, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

        def cb(_h, _dc, rect, _lp):
            r = rect.contents
            out.append({"left": r.left, "top": r.top, "width": r.right - r.left, "height": r.bottom - r.top})
            return 1
        ctypes.windll.user32.EnumDisplayMonitors(None, None, proc(cb), 0)
        return sorted(out, key=lambda m: (m["left"], m["top"]))
    img, region = screenshot(0)
    return [{"left": 0, "top": 0, "width": region["width"], "height": region["height"]}]


def camera_frame(index: int = 0, warmup: int = 5) -> np.ndarray:
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY)
    try:
        if not cap.isOpened():
            raise VisionError(f"camera {index} is not available")
        frame = None
        for _ in range(max(1, warmup)):          # the first frames of many cameras are dark while exposure settles
            ok, f = cap.read()
            if ok:
                frame = f
            time.sleep(0.03)
        if frame is None:
            raise VisionError(f"camera {index} gave no frame")
        return frame
    finally:
        cap.release()


def load(src: Any) -> tuple[np.ndarray, dict]:
    """(image in BGR, where it came from). Accepts a source string (see the module docstring) or an array."""
    if isinstance(src, np.ndarray):
        return _checked(src if src.ndim == 3 else cv2.cvtColor(src, cv2.COLOR_GRAY2BGR), "the image"), {"kind": "array"}
    if not isinstance(src, str) or not src.strip():
        raise VisionError("give an image: a file path, an http(s) URL, a data: URL, 'screen' or 'camera'")
    s = src.strip()
    low = s.lower()
    if low == "screen" or low.startswith("screen:"):
        n = int(low.split(":", 1)[1]) if ":" in low else 0
        img, region = screenshot(n)
        return img, {"kind": "screen", **region}
    if low == "camera" or low.startswith("camera:"):
        n = int(low.split(":", 1)[1]) if ":" in low else 0
        return camera_frame(n), {"kind": "camera", "index": n}
    if low.startswith("data:"):
        return _from_data_url(s), {"kind": "data-url"}
    if low.startswith(("http://", "https://")):
        return _from_url(s), {"kind": "url", "url": s}
    p = Path(s).expanduser()
    if not p.is_file():
        raise VisionError(f"{p}: no such file")
    if p.stat().st_size > MAX_BYTES:
        raise VisionError(f"{p} is larger than {MAX_BYTES >> 20} MB")
    return _decode(p.read_bytes(), str(p)), {"kind": "file", "path": str(p.resolve())}


def save(img: np.ndarray, name: str, ext: str = ".png") -> str:
    """Write a result image to data/vision/out and return its path."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:60] or "image"
    path = out_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}-{safe}{ext}"
    if not cv2.imwrite(str(path), img):
        raise VisionError(f"could not write {path}")
    return str(path)


def encode(img: np.ndarray, ext: str = ".png", max_side: int = 0) -> bytes:
    if max_side and max(img.shape[:2]) > max_side:
        k = max_side / max(img.shape[:2])
        img = cv2.resize(img, (max(1, int(img.shape[1] * k)), max(1, int(img.shape[0] * k))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        raise VisionError(f"could not encode as {ext}")
    return buf.tobytes()


def data_url(img: np.ndarray, max_side: int = 1600) -> str:
    return "data:image/png;base64," + base64.b64encode(encode(img, ".png", max_side)).decode("ascii")

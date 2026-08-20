"""Best-effort JPEG thumbnails for image attachments, generated once at
upload time. Pillow's decode/resize is real CPU work, so every caller runs
this via run_in_executor — never on the shared event loop the Telegram
poller also depends on.

Deliberately image-only: video/audio thumbnailing would need an ffmpeg
dependency this personal, single-operator server doesn't otherwise need.
Clients fall back to a generic file-type icon for anything this returns
None for (non-image mime, corrupt/unsupported image), so a decode failure
is never fatal to the upload itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image, UnidentifiedImageError

MAX_DIMENSION = 320
JPEG_QUALITY = 70


def generate_thumbnail(source_path: Path, mime: Optional[str], thumbs_dir: Path) -> Optional[Path]:
    if not mime or not mime.startswith("image/"):
        return None
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    dest = thumbs_dir / f"{source_path.stem}.jpg"
    try:
        with Image.open(source_path) as img:
            img = img.convert("RGB")
            img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
            img.save(dest, "JPEG", quality=JPEG_QUALITY)
    except (UnidentifiedImageError, OSError):
        return None
    return dest

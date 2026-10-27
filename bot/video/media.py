"""The media layer: ffmpeg for transcode, trim, concat, audio mux, frames <-> video, thumbnails and previews; ffprobe
for metadata.

Which ffmpeg: `video.ffmpeg` (a path), else $ABP_FFMPEG, else the one on PATH. When `video.ffmpeg_sha256` is set the
binary must hash to it (pinned and verified), else nothing runs. Every operation is built as an argument list
(`*_args`, tested on their own) and run without a shell. Without ffmpeg, `probe` and `thumbnail` fall back to OpenCV
so the library still shows clips; the rest say plainly that ffmpeg is needed."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from bot.video import store


class MediaError(RuntimeError):
    pass


def _setting(key: str) -> str:
    from bot.video import jobs
    return str(jobs.settings().get(key) or "").strip()


def ffmpeg_path() -> Optional[str]:
    p = _setting("ffmpeg") or os.environ.get("ABP_FFMPEG", "")
    if p:
        return p if Path(p).is_file() else None
    return shutil.which("ffmpeg")


def ffprobe_path() -> Optional[str]:
    ff = ffmpeg_path()
    if not ff:
        return None
    p = Path(ff)
    sib = p.with_name(p.name.replace("ffmpeg", "ffprobe"))
    return str(sib) if sib.is_file() and sib != p else shutil.which("ffprobe")


_verified: dict[str, str] = {}


def status() -> dict:
    ff = ffmpeg_path()
    pin = _setting("ffmpeg_sha256").lower()
    out: dict[str, Any] = {"ffmpeg": ff, "ffprobe": ffprobe_path(), "pinned": bool(pin), "verified": None}
    if ff and pin:
        out["verified"] = _sha(ff) == pin
    return out


def _sha(path: str) -> str:
    st = os.stat(path)
    key = f"{path}|{st.st_size}|{st.st_mtime}"
    if key not in _verified:
        _verified[key] = store.sha256_file(Path(path))
    return _verified[key]


def require() -> str:
    ff = ffmpeg_path()
    if not ff:
        raise MediaError("ffmpeg is not installed or not found; set its path in the Video Studio settings")
    pin = _setting("ffmpeg_sha256").lower()
    if pin and _sha(ff) != pin:
        raise MediaError(f"{ff} does not match the pinned SHA-256; refusing to run it")
    return ff


def run(args: list[str], *, timeout: float = 3600.0, tool: str = "ffmpeg") -> subprocess.CompletedProcess:
    exe = require() if tool == "ffmpeg" else (ffprobe_path() or "")
    if not exe:
        raise MediaError("ffprobe was not found next to ffmpeg or on PATH")
    if tool == "ffprobe":
        require()                                    # the pin covers the pair
    cp = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if cp.returncode != 0:
        raise MediaError(f"{tool} failed ({cp.returncode}): {(cp.stderr or '').strip()[-600:]}")
    return cp


# ---- argument builders (pure) -------------------------------------------------------------------------------------
_Q = ["-hide_banner", "-loglevel", "error", "-y"]


def _codec(dst: Path) -> list[str]:
    ext = dst.suffix.lower()
    if ext == ".webm":
        return ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-row-mt", "1"]
    if ext in (".mp4", ".mov", ".m4v", ".mkv"):
        return ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "medium"]
    if ext == ".gif":
        return []
    raise MediaError(f"unsupported output type {ext}")


def testsrc_args(dst: Path, *, seconds: float, fps: int, width: int, height: int, text: str = "") -> list[str]:
    vf = []
    if text:
        safe = text.replace("\\", "").replace(":", " ").replace("'", "").replace("%", "")[:60]
        vf = ["-vf", f"drawtext=text='{safe}':x=8:y=h-th-8:fontcolor=white:box=1:boxcolor=black@0.6"]
    return [*_Q, "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate={fps}:duration={seconds}", *vf,
            *_codec(dst), str(dst)]


def transcode_args(src: Path, dst: Path, *, width: int = 0, fps: int = 0) -> list[str]:
    vf = ",".join(x for x in (f"scale={width}:-2" if width else "", f"fps={fps}" if fps else "") if x)
    return [*_Q, "-i", str(src), *(["-vf", vf] if vf else []), *_codec(dst), "-c:a",
            "libopus" if dst.suffix.lower() == ".webm" else "aac", str(dst)]


def trim_args(src: Path, dst: Path, *, start: float, end: float) -> list[str]:
    if end <= start:
        raise MediaError("trim: end must be after start")
    return [*_Q, "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src), *_codec(dst), str(dst)]


def concat_list(srcs: list[Path], listfile: Path) -> None:
    listfile.write_text("".join("file '" + str(s).replace("'", "'\\''") + "'\n" for s in srcs), encoding="utf-8")


def concat_args(listfile: Path, dst: Path) -> list[str]:
    return [*_Q, "-f", "concat", "-safe", "0", "-i", str(listfile), *_codec(dst), str(dst)]


def mux_audio_args(video: Path, audio: Path, dst: Path) -> list[str]:
    return [*_Q, "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a",
            "libopus" if dst.suffix.lower() == ".webm" else "aac", "-shortest", str(dst)]


def frames_to_video_args(pattern: str, dst: Path, *, fps: int) -> list[str]:
    return [*_Q, "-framerate", str(fps), "-i", pattern, *_codec(dst), str(dst)]


def video_to_frames_args(src: Path, pattern: str, *, fps: int = 0) -> list[str]:
    return [*_Q, "-i", str(src), *(["-vf", f"fps={fps}"] if fps else []), pattern]


def thumbnail_args(src: Path, dst: Path, *, at: float = 0.0, width: int = 320) -> list[str]:
    return [*_Q, "-ss", f"{at:.3f}", "-i", str(src), "-frames:v", "1", "-vf", f"scale={width}:-2", str(dst)]


def preview_args(src: Path, dst: Path, *, width: int = 320, fps: int = 8) -> list[str]:
    return [*_Q, "-i", str(src), "-vf", f"fps={fps},scale={width}:-2", "-an", *_codec(dst), str(dst)]


PROBE_ARGS = ["-v", "error", "-print_format", "json", "-show_format", "-show_streams"]


# ---- operations ---------------------------------------------------------------------------------------------------
def testsrc(dst: Path, **kw: Any) -> Path:
    run(testsrc_args(dst, **kw))
    return dst


def transcode(src: Path, dst: Path, **kw: Any) -> Path:
    run(transcode_args(src, dst, **kw))
    return dst


def trim(src: Path, dst: Path, **kw: Any) -> Path:
    run(trim_args(src, dst, **kw))
    return dst


def concat(srcs: list[Path], dst: Path) -> Path:
    if not srcs:
        raise MediaError("concat: no inputs")
    lf = dst.with_name(dst.name + ".txt")
    concat_list(srcs, lf)
    try:
        run(concat_args(lf, dst))
    finally:
        lf.unlink(missing_ok=True)
    return dst


def mux_audio(video: Path, audio: Path, dst: Path) -> Path:
    run(mux_audio_args(video, audio, dst))
    return dst


def frames_to_video(pattern: str, dst: Path, *, fps: int) -> Path:
    run(frames_to_video_args(pattern, dst, fps=fps))
    return dst


def video_to_frames(src: Path, pattern: str, *, fps: int = 0) -> str:
    run(video_to_frames_args(src, pattern, fps=fps))
    return pattern


def probe(src: Path) -> dict:
    """{duration, width, height, fps, frames, codec, audio, bytes}: ffprobe when present, else OpenCV."""
    src = Path(src)
    if ffprobe_path():
        try:
            data = json.loads(run([*PROBE_ARGS, str(src)], tool="ffprobe", timeout=60).stdout or "{}")
            return parse_probe(data, src)
        except MediaError:
            pass
    return _cv_probe(src)


def parse_probe(data: dict, src: Path) -> dict:
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    num, _, den = str(v.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    dur = float((data.get("format") or {}).get("duration") or v.get("duration") or 0)
    return {"duration": round(dur, 3), "width": v.get("width"), "height": v.get("height"), "fps": round(fps, 3),
            "frames": int(v.get("nb_frames") or round(dur * fps)), "codec": v.get("codec_name"), "audio": a,
            "bytes": src.stat().st_size if src.is_file() else None, "via": "ffprobe"}


def _cv_probe(src: Path) -> dict:
    import cv2
    cap = cv2.VideoCapture(str(src))
    try:
        if not cap.isOpened():
            raise MediaError(f"{src.name}: not a video OpenCV can read")
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        return {"duration": round(n / fps, 3) if fps else 0.0, "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)), "fps": round(fps, 3), "frames": n, "codec": None,
                "audio": None, "bytes": src.stat().st_size, "via": "opencv"}
    finally:
        cap.release()


def thumbnail(src: Path, dst: Path, *, at: float = 0.0, width: int = 320) -> Path:
    if ffmpeg_path():
        try:
            run(thumbnail_args(src, dst, at=at, width=width), timeout=120)
            return dst
        except MediaError:
            pass
    import cv2
    cap = cv2.VideoCapture(str(src))
    try:
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok:
        raise MediaError(f"{Path(src).name}: no frame to make a thumbnail from")
    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, max(2, int(h * width / w))))
    if not cv2.imwrite(str(dst), frame):
        raise MediaError("could not write the thumbnail")
    return dst


def preview(src: Path, dst: Path, **kw: Any) -> Path:
    run(preview_args(src, dst, **kw))
    return dst

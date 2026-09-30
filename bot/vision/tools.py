"""The agent's vision tools (OpenCV and its model zoo, running on this machine; no image leaves it).

    vision_analyze   what is in an image: objects, faces, text, QR codes and barcodes, people, colours, shapes, info
    vision_find      where a piece of text or a smaller image is on an image or on the screen (positions to click)
    vision_compare   what changed between two images (a UI before and after a change)
    vision_edit      resize, crop, rotate, blur, sharpen, edges, threshold... and save the result
    vision_faces     whether two images show the same person
    vision_capture   one frame from a camera, saved to a file (always asks first)
    vision_status    the device, the models (downloaded or not), the tasks and edits available

Images are file paths, http(s) URLs, data: URLs, or "screen" / "screen:<n>". The camera is only reachable through
vision_capture, which asks the person every time.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

# OpenCV (and numpy) load on a tool's first call, in the worker thread that runs it: registering the tools must not
# pull them into every agent process (abp_acp's turns hung importing numpy behind its stdin reader on Windows).
TASKS = ("info", "faces", "objects", "text", "codes", "people", "colors", "shapes")     # = service.TASKS (tested)
MAX_OUT = 14_000
IMAGE = {"type": "string", "description": "a file path, an http(s) URL, a data: URL, or \"screen\" (every monitor) / "
                                          "\"screen:<n>\" (monitor n)"}


def _out(value: Any) -> str:
    text = json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


def _camera_refused(*sources: Any) -> str:
    for s in sources:
        if isinstance(s, str) and s.strip().lower().startswith("camera"):
            return ("Error: the camera is reached only through vision_capture (it asks the person first); capture a "
                    "frame there, then pass its path")
    return ""


def _in_thread(name: str, *args, **kwargs) -> Any:
    from bot.vision import service
    return getattr(service, name)(*args, **kwargs)


def _capture(camera: int) -> dict:
    from bot.vision import images
    img, origin = images.load(f"camera:{camera}")
    return {"path": images.save(img, "camera"), "size": [img.shape[1], img.shape[0]], **origin}


async def _call(name: str, *args, **kwargs) -> str:
    fn = _capture if name == "capture" else _in_thread
    try:
        return _out(await asyncio.to_thread(fn, *args, **kwargs) if name == "capture"
                    else await asyncio.to_thread(fn, name, *args, **kwargs))
    except Exception as e:  # noqa: BLE001 - a VisionError or an OpenCV error must reach the agent as text
        return f"Error: {e}" if type(e).__name__ == "VisionError" else f"Error: {type(e).__name__}: {e}"


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    def reg(name, description, props, required, handler, *, read_only=True, needs_approval=None):
        toolspec.register(
            {"name": name, "description": description,
             "input_schema": {"type": "object", "properties": props, "required": required}},
            toolspec.ToolSpec(name, "read" if read_only else "external", read_only=read_only,
                              concurrency_safe=read_only, origin="registered", needs_approval=needs_approval),
            handler)

    async def analyze(inp, **_):
        if refused := _camera_refused(inp.get("image")):
            return refused
        return await _call("analyze", inp.get("image"), inp.get("tasks"),
                           min_score=float(inp.get("min_score") or 0.4), classes=inp.get("classes"))

    async def find(inp, **_):
        if refused := _camera_refused(inp.get("image"), inp.get("template")):
            return refused
        return await _call("find", inp.get("image") or "screen", text=str(inp.get("text") or ""),
                           template=inp.get("template"), threshold=float(inp.get("threshold") or 0.8))

    async def compare(inp, **_):
        if refused := _camera_refused(inp.get("before"), inp.get("after")):
            return refused
        return await _call("compare", inp.get("before"), inp.get("after"))

    async def edit(inp, **_):
        if refused := _camera_refused(inp.get("image")):
            return refused
        return await _call("edit", inp.get("image"), inp.get("steps") or [])

    async def faces(inp, **_):
        if refused := _camera_refused(inp.get("a"), inp.get("b")):
            return refused
        return await _call("faces_match", inp.get("a"), inp.get("b"))

    async def capture(inp, **_):
        return await _call("capture", int(inp.get("camera") or 0))

    async def status(inp, **_):
        return await _call("status")

    reg("vision_analyze", "See what is in an image, on this machine with OpenCV and its model zoo. tasks (any of): "
        "objects (80 everyday classes: people, vehicles, animals, furniture, electronics, food...; box, label, "
        "score), faces (box, five landmarks), text (OCR, line by line, with each line's box and confidence; Latin "
        "letters, digits and punctuation), codes (QR codes and barcodes, decoded), people (which pixels are people), "
        "colors (dominant colours), shapes (outlines of regions), info (size, brightness, contrast, sharpness). "
        "Default: info, objects, faces, text, codes. On a screenshot every item also has screen_center (where to "
        "click). 'annotated' is a picture with the results drawn on it.",
        {"image": IMAGE, "tasks": {"type": "array", "items": {"type": "string", "enum": list(TASKS)}},
         "min_score": {"type": "number", "description": "0..1, default 0.4"},
         "classes": {"type": "array", "items": {"type": "string"}, "description": "only these object classes"}},
        ["image"], analyze)
    reg("vision_find", "Find where something is on an image or on the screen (default): text (case-insensitive, "
        "read with OCR) or template (a smaller image, e.g. a cropped button; tried at several sizes, and with "
        "feature matching if it is rotated or seen at an angle). Each match: box, center and, on the screen, "
        "screen_center (the position to click).",
        {"image": IMAGE, "text": {"type": "string"}, "template": IMAGE,
         "threshold": {"type": "number", "description": "template match score 0..1, default 0.8"}},
        [], find)
    reg("vision_compare", "What changed between two images, e.g. a page before and after a change: similarity (1.0 = "
        "identical), the share of pixels changed, the changed regions (boxes), and 'annotated', the after image "
        "with the changes outlined.", {"before": IMAGE, "after": IMAGE}, ["before", "after"], compare)
    reg("vision_edit", "Edit an image and save the result (its path is returned). steps, applied in order, each "
        "{\"op\": ...} with its parameters: resize (width, height or scale), crop (x, y, width, height), rotate "
        "(angle), flip (direction: horizontal|vertical|both), gray, blur (radius), sharpen (amount), edges (low, "
        "high), threshold (mode: otsu|adaptive), invert, brightness (brightness, contrast), denoise (strength).",
        {"image": IMAGE, "steps": {"type": "array", "items": {"type": "object"}}}, ["image", "steps"], edit)
    reg("vision_faces", "Whether two images show the same person (the largest face in each; SFace, cosine similarity "
        "against the zoo's threshold).", {"a": IMAGE, "b": IMAGE}, ["a", "b"], faces)
    reg("vision_capture", "Take one frame from a camera (default camera 0) and save it; returns its path for the "
        "other vision tools. The person is asked every time.", {"camera": {"type": "integer"}}, [], capture,
        read_only=False, needs_approval=True)
    reg("vision_status", "Vision on this machine: which device runs the models, which zoo models are downloaded "
        "(each is fetched and verified on first use), the analysis tasks and edits available, and where results are "
        "saved.", {}, [], status)


register_tools()

"""Vision's front door: what the agent tools, /api/vision, the CLI and MCP all call, so they give the same answers.

Every function takes image sources (see images.py) and returns a JSON-able dict. When a result is easier to see than
to read (boxes on an image, a before/after difference) the dict carries "annotated": the path of that picture, under
data/vision/out."""
from __future__ import annotations

import time
from typing import Any, Optional

from bot.vision import dnn, images, ops, pipelines, zoo
from bot.vision.images import VisionError

TASKS = ("info", "faces", "objects", "text", "codes", "people", "colors", "shapes")
LOW_TEXT_CONFIDENCE = 0.6


def _screen_pos(item: dict, origin: dict) -> dict:
    """For a screenshot: add where the item is on the screen (the virtual desktop), for clicking."""
    if origin.get("kind") != "screen" or "box" not in item:
        return item
    x, y, w, h = item["box"]
    return {**item, "screen_center": [origin.get("left", 0) + x + w // 2, origin.get("top", 0) + y + h // 2]}


def analyze(source: Any, tasks: Optional[list[str]] = None, *, min_score: float = 0.4, classes: Optional[list[str]] = None,
            annotate: bool = True) -> dict:
    """Run several analyses on one image. tasks: any of TASKS (default: info, objects, faces, text, codes)."""
    img, origin = images.load(source)
    tasks = [t for t in (tasks or ["info", "objects", "faces", "text", "codes"])]
    bad = [t for t in tasks if t not in TASKS]
    if bad:
        raise VisionError(f"unknown task(s) {bad}; known: {', '.join(TASKS)}")
    out: dict[str, Any] = {"source": origin, "size": [img.shape[1], img.shape[0]], "timings_ms": {}}
    drawn: list[dict] = []
    for t in tasks:
        t0 = time.perf_counter()
        if t == "info":
            out["info"] = ops.info(img)
        elif t == "faces":
            found = pipelines.public(pipelines.faces(img, max(0.3, min_score)))
            out["faces"] = [_screen_pos(f, origin) for f in found]
            drawn += found
        elif t == "objects":
            found = pipelines.objects(img, min_score, classes)
            out["objects"] = [_screen_pos(o, origin) for o in found]
            drawn += found
        elif t == "text":
            lines = [_screen_pos(r, origin) for r in pipelines.text(img)]
            out["text"] = lines
            out["text_joined"] = "\n".join(r["text"] for r in lines if r["confidence"] >= LOW_TEXT_CONFIDENCE)
            if any(r["confidence"] < LOW_TEXT_CONFIDENCE for r in lines):
                out["text_note"] = (f"lines under {LOW_TEXT_CONFIDENCE} confidence are left out of text_joined: usually "
                                    "a script the recognizer does not read (it reads Latin letters, digits and "
                                    "punctuation) or text too small or blurred")
            drawn += [{"polygon": r["polygon"], "text": r["text"]} for r in lines]
        elif t == "codes":
            found = pipelines.codes(img)
            out["codes"] = [_screen_pos(c, origin) for c in found]
            drawn += [{"polygon": c["polygon"], "label": c["kind"]} for c in found]
        elif t == "people":
            info, _mask = pipelines.people_mask(img)
            out["people"] = info
            drawn += info["regions"]
        elif t == "colors":
            out["colors"] = ops.colors(img)
        elif t == "shapes":
            out["shapes"] = ops.contours(img)[:50]
        out["timings_ms"][t] = round((time.perf_counter() - t0) * 1000, 1)
    if annotate and drawn:
        out["annotated"] = images.save(ops.draw(img, drawn), "analysis")
    return out


def find(source: Any, *, text: str = "", template: Any = None, threshold: float = 0.8,
         scales: Optional[list[float]] = None) -> dict:
    """Where something is on an image (or "screen"): a piece of text (case-insensitive, OCR), or a smaller image
    (template matching at several scales; with rotation or perspective, feature matching). Each match has a box and a
    center; on a screenshot also screen_center, the position to click."""
    img, origin = images.load(source)
    matches: list[dict] = []
    how = ""
    if text:
        how = "text"
        want = text.lower().strip()
        for r in pipelines.text(img):
            if want in r["text"].lower():
                matches.append({"text": r["text"], "box": r["box"], "confidence": r["confidence"],
                                "center": [r["box"][0] + r["box"][2] // 2, r["box"][1] + r["box"][3] // 2]})
    elif template is not None:
        how = "template"
        templ, _ = images.load(template)
        matches = ops.match_template(img, templ, threshold, scales or [0.5, 0.75, 1.0, 1.25, 1.5, 2.0])
        if not matches:
            hit = ops.match_features(img, templ)
            if hit:
                how = "features"
                matches = [hit]
    else:
        raise VisionError("give text to look for, or a template image")
    matches = [_screen_pos(m, origin) for m in matches]
    res: dict[str, Any] = {"source": origin, "by": how, "found": len(matches), "matches": matches}
    if matches:
        res["annotated"] = images.save(ops.draw(img, [{"box": m["box"], "label": m.get("text") or "match"}
                                                     for m in matches]), "find")
    return res


def compare(before: Any, after: Any, threshold: int = 25) -> dict:
    """What changed between two images: similarity (1.0 = identical), the share of pixels that changed, the changed
    regions, and a picture of `after` with them outlined."""
    a, oa = images.load(before)
    b, ob = images.load(after)
    res, shown = ops.compare(a, b, threshold)
    return {"before": oa, "after": ob, **res, "annotated": images.save(shown, "compare")}


def edit(source: Any, steps: list[dict]) -> dict:
    """Apply edits in order (each {"op": one of ops.TRANSFORMS, ...its parameters}) and save the result."""
    img, origin = images.load(source)
    if not steps:
        raise VisionError("give at least one step, e.g. [{\"op\": \"resize\", \"width\": 800}]")
    for s in steps:
        img = ops.transform(img, str(s.get("op") or ""), {k: v for k, v in s.items() if k != "op"})
    return {"source": origin, "size": [img.shape[1], img.shape[0]], "path": images.save(img, "edit")}


def faces_match(a: Any, b: Any) -> dict:
    ia, _ = images.load(a)
    ib, _ = images.load(b)
    return pipelines.same_person(ia, ib)


def status() -> dict:
    return {"device": dnn.status(), "models": zoo.status(), "tasks": list(TASKS), "edits": list(ops.TRANSFORMS),
            "output_folder": str(images.out_dir())}

"""Image operations that need no model. Each takes BGR images and returns JSON-able results (and, where it makes one,
an image), so agents, the API and the CLI all get the same answers."""
from __future__ import annotations

from typing import Any, Optional

import cv2
import numpy as np

from bot.vision.images import VisionError


def info(img: np.ndarray) -> dict:
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return {"width": w, "height": h, "channels": 1 if img.ndim == 2 else img.shape[2],
            "brightness": round(float(gray.mean()) / 255, 3), "contrast": round(float(gray.std()) / 128, 3),
            "sharpness": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 1),
            "mean_bgr": [round(float(x), 1) for x in img.reshape(-1, img.shape[2]).mean(axis=0)]}


def colors(img: np.ndarray, k: int = 5) -> list[dict]:
    """The dominant colours (k-means on a downscaled copy): hex, RGB and share of the image."""
    small = cv2.resize(img, (160, max(1, int(160 * img.shape[0] / img.shape[1]))), interpolation=cv2.INTER_AREA)
    data = small.reshape(-1, 3).astype(np.float32)
    k = max(1, min(int(k), 12, len(data)))
    _, labels, centers = cv2.kmeans(data, k, None, (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0), 3,
                                    cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.ravel(), minlength=k)
    out = []
    for i in np.argsort(-counts):
        b, g, r = (int(round(x)) for x in centers[i])
        out.append({"hex": f"#{r:02x}{g:02x}{b:02x}", "rgb": [r, g, b], "share": round(float(counts[i]) / len(data), 3)})
    return out


def histogram(img: np.ndarray, bins: int = 32) -> dict:
    bins = max(4, min(int(bins), 256))
    return {name: [int(x) for x in cv2.calcHist([img], [i], None, [bins], [0, 256]).ravel()]
            for i, name in enumerate(("blue", "green", "red"))}


def _odd(n: int) -> int:
    n = max(1, int(n))
    return n if n % 2 else n + 1


def transform(img: np.ndarray, op: str, p: Optional[dict] = None) -> np.ndarray:
    """One edit: resize, crop, rotate, flip, gray, blur, sharpen, edges, threshold, invert, brightness, denoise."""
    p = p or {}
    h, w = img.shape[:2]
    if op == "resize":
        if p.get("scale"):
            s = float(p["scale"])
            nw, nh = int(w * s), int(h * s)
        else:
            nw, nh = int(p.get("width") or 0), int(p.get("height") or 0)
            if nw and not nh:
                nh = int(h * nw / w)
            elif nh and not nw:
                nw = int(w * nh / h)
        if nw <= 0 or nh <= 0 or nw * nh > 80_000_000:
            raise VisionError("resize: give width and/or height, or scale")
        return cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if nw * nh < w * h else cv2.INTER_CUBIC)
    if op == "crop":
        x, y, cw, ch = (int(p.get(k, 0)) for k in ("x", "y", "width", "height"))
        x, y = max(0, x), max(0, y)
        cw, ch = min(cw or w - x, w - x), min(ch or h - y, h - y)
        if cw <= 0 or ch <= 0:
            raise VisionError("crop: the region is outside the image")
        return img[y:y + ch, x:x + cw].copy()
    if op == "rotate":
        angle = float(p.get("angle", 90))
        if angle % 90 == 0:
            k = int(angle // 90) % 4
            return img if k == 0 else cv2.rotate(img, [None, cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_180,
                                                       cv2.ROTATE_90_CLOCKWISE][k])
        m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        cos, sin = abs(m[0, 0]), abs(m[0, 1])
        nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
        m[0, 2] += nw / 2 - w / 2
        m[1, 2] += nh / 2 - h / 2
        return cv2.warpAffine(img, m, (nw, nh), borderValue=(255, 255, 255))
    if op == "flip":
        return cv2.flip(img, {"horizontal": 1, "vertical": 0, "both": -1}[p.get("direction", "horizontal")])
    if op == "gray":
        return cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    if op == "blur":
        return cv2.GaussianBlur(img, (_odd(p.get("radius", 5) * 2 + 1),) * 2, 0)
    if op == "sharpen":
        amount = float(p.get("amount", 1.0))
        soft = cv2.GaussianBlur(img, (0, 0), 3)
        return cv2.addWeighted(img, 1 + amount, soft, -amount, 0)
    if op == "edges":
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        lo, hi = int(p.get("low", 50)), int(p.get("high", 150))
        return cv2.cvtColor(cv2.Canny(gray, lo, hi), cv2.COLOR_GRAY2BGR)
    if op == "threshold":
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if p.get("mode", "otsu") == "adaptive":
            out = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,
                                        _odd(p.get("block", 31)), int(p.get("c", 10)))
        else:
            _, out = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)
    if op == "invert":
        return cv2.bitwise_not(img)
    if op == "brightness":
        return cv2.convertScaleAbs(img, alpha=float(p.get("contrast", 1.0)), beta=float(p.get("brightness", 0)))
    if op == "denoise":
        return cv2.fastNlMeansDenoisingColored(img, None, float(p.get("strength", 7)), float(p.get("strength", 7)), 7, 21)
    raise VisionError(f"unknown operation {op!r} (resize, crop, rotate, flip, gray, blur, sharpen, edges, threshold, "
                      "invert, brightness, denoise)")


TRANSFORMS = ("resize", "crop", "rotate", "flip", "gray", "blur", "sharpen", "edges", "threshold", "invert",
              "brightness", "denoise")


def contours(img: np.ndarray, min_area: float = 100.0, limit: int = 200) -> list[dict]:
    """Shapes: the outlines of distinct regions, largest first, with their box, area and a rough shape name."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(cv2.GaussianBlur(gray, (5, 5), 0), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if bw.mean() > 127:
        bw = cv2.bitwise_not(bw)
    found, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in sorted(found, key=cv2.contourArea, reverse=True):
        area = cv2.contourArea(c)
        if area < min_area:
            break
        x, y, w, h = cv2.boundingRect(c)
        perimeter = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * perimeter, True)
        n = len(approx)
        circularity = 4 * np.pi * area / max(perimeter * perimeter, 1e-9)     # 1.0 for a perfect circle
        if n > 6 and circularity > 0.85:
            shape = "circle" if 0.9 <= w / max(h, 1) <= 1.1 else "ellipse"
        else:
            shape = {3: "triangle", 4: "rectangle" if 0.9 > w / max(h, 1) or w / max(h, 1) > 1.1 else "square",
                     5: "pentagon", 6: "hexagon"}.get(n, f"{n}-gon")
        out.append({"box": [x, y, w, h], "area": round(float(area), 1), "shape": shape, "vertices": n})
        if len(out) >= limit:
            break
    return out


def match_template(img: np.ndarray, templ: np.ndarray, threshold: float = 0.8, scales: Optional[list[float]] = None,
                   limit: int = 20) -> list[dict]:
    """Where a smaller image appears in a bigger one (a button on a screenshot): boxes with a score 0..1, best first.
    scales tries the template at several sizes (e.g. [0.75, 1, 1.25, 1.5, 2] for different display scaling)."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    t0 = cv2.cvtColor(templ, cv2.COLOR_BGR2GRAY)
    boxes, scores = [], []
    for s in scales or [1.0]:
        t = t0 if s == 1.0 else cv2.resize(t0, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
        th, tw = t.shape[:2]
        if th > g.shape[0] or tw > g.shape[1] or th < 4 or tw < 4:
            continue
        res = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= threshold)
        for y, x in zip(ys, xs):
            boxes.append([int(x), int(y), tw, th])
            scores.append(float(res[y, x]))
    if not boxes:
        return []
    keep = cv2.dnn.NMSBoxes(boxes, scores, threshold, 0.3)
    keep = sorted((int(i) for i in np.array(keep).ravel()), key=lambda i: -scores[i])[:limit]
    return [{"box": boxes[i], "center": [boxes[i][0] + boxes[i][2] // 2, boxes[i][1] + boxes[i][3] // 2],
             "score": round(scores[i], 3)} for i in keep]


def match_features(img: np.ndarray, obj: np.ndarray, min_matches: int = 10) -> Optional[dict]:
    """Find an object that may be rotated, scaled or seen at an angle (SIFT + RANSAC homography): its outline in the
    image, or None."""
    sift = cv2.SIFT_create()
    k1, d1 = sift.detectAndCompute(cv2.cvtColor(obj, cv2.COLOR_BGR2GRAY), None)
    k2, d2 = sift.detectAndCompute(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), None)
    if d1 is None or d2 is None or len(k1) < 2 or len(k2) < 2:
        return None
    pairs = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 5}, {"checks": 50}).knnMatch(d1, d2, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.7 * n.distance]
    if len(good) < min_matches:
        return None
    src = np.float32([k1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([k2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    hmat, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if hmat is None:
        return None
    h, w = obj.shape[:2]
    quad = cv2.perspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2), hmat).reshape(-1, 2)
    x, y, bw, bh = cv2.boundingRect(quad.astype(np.float32))
    return {"corners": [[round(float(a), 1), round(float(b), 1)] for a, b in quad], "box": [x, y, bw, bh],
            "center": [x + bw // 2, y + bh // 2], "matches": len(good), "inliers": int(mask.sum())}


def compare(a: np.ndarray, b: np.ndarray, threshold: int = 25, min_area: int = 16) -> tuple[dict, np.ndarray]:
    """What changed between two images (a UI before and after a change): a similarity score, the changed share of the
    image, and the changed regions; plus an image with the changes outlined on b."""
    if a.shape[:2] != b.shape[:2]:
        a = cv2.resize(a, (b.shape[1], b.shape[0]), interpolation=cv2.INTER_AREA)
    ga, gb = (cv2.GaussianBlur(cv2.cvtColor(x, cv2.COLOR_BGR2GRAY), (3, 3), 0) for x in (a, b))
    diff = cv2.absdiff(ga, gb)
    _, mask = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=2)
    found, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    regions = []
    shown = b.copy()
    for c in sorted(found, key=cv2.contourArea, reverse=True):
        if cv2.contourArea(c) < min_area:
            continue
        x, y, w, h = cv2.boundingRect(c)
        regions.append({"box": [x, y, w, h], "mean_change": round(float(diff[y:y + h, x:x + w].mean()) / 255, 3)})
        cv2.rectangle(shown, (x, y), (x + w, y + h), (0, 0, 255), 2)
    ssim = _ssim(ga, gb)
    return ({"similarity": round(ssim, 4), "changed_share": round(float((mask > 0).mean()), 4),
             "identical": bool(ssim > 0.9999 and not regions), "regions": regions[:100]}, shown)


def _ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Structural similarity (Wang et al. 2004) over the whole image, 1.0 for identical."""
    a, b = a.astype(np.float64), b.astype(np.float64)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mu_a, mu_b = cv2.GaussianBlur(a, (11, 11), 1.5), cv2.GaussianBlur(b, (11, 11), 1.5)
    s_a = cv2.GaussianBlur(a * a, (11, 11), 1.5) - mu_a ** 2
    s_b = cv2.GaussianBlur(b * b, (11, 11), 1.5) - mu_b ** 2
    s_ab = cv2.GaussianBlur(a * b, (11, 11), 1.5) - mu_a * mu_b
    m = ((2 * mu_a * mu_b + c1) * (2 * s_ab + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (s_a + s_b + c2))
    return float(m.mean())


def draw(img: np.ndarray, items: list[dict[str, Any]], color=(0, 200, 0)) -> np.ndarray:
    """Boxes (and polygons) with labels, for any result that has "box" or "polygon" and an optional "label"."""
    out = img.copy()
    t = max(1, int(round(max(img.shape[:2]) / 700)))
    for it in items:
        label = str(it.get("label") or it.get("text") or "")
        if "score" in it and label:
            label += f" {float(it['score']):.2f}"
        if it.get("polygon"):
            pts = np.array(it["polygon"], np.int32).reshape(-1, 1, 2)
            cv2.polylines(out, [pts], True, color, t + 1)
            x, y = int(pts[:, 0, 0].min()), int(pts[:, 0, 1].min())
        elif it.get("box"):
            x, y, w, h = (int(v) for v in it["box"])
            cv2.rectangle(out, (x, y), (x + w, y + h), color, t + 1)
        else:
            continue
        if label:
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45 * t, t)
            cv2.rectangle(out, (x, max(0, y - th - 6)), (x + tw + 4, y), color, -1)
            cv2.putText(out, label, (x + 2, max(th, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45 * t, (0, 0, 0), t, cv2.LINE_AA)
    return out

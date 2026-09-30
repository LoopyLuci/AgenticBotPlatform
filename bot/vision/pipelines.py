"""What the zoo's models do, the way the zoo's own demos run them (same pre- and post-processing and thresholds).

Every result is JSON-able: boxes are [x, y, width, height] in the image's pixels, polygons are lists of [x, y], scores
are 0..1."""
from __future__ import annotations

import string
import threading
from typing import Any, Callable

import cv2
import numpy as np

from bot.vision import dnn, zoo

COCO = ("person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
        "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow", "elephant",
        "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis",
        "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
        "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
        "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
        "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
        "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
        "hair drier", "toothbrush")
CHARSET_94 = string.printable[:94]           # the CRNN "CH" model's alphabet: digits, letters, punctuation
FACE_MATCH_COSINE = 0.363                    # SFace: the zoo's threshold for "the same person"

_cache: dict[tuple[str, int], Any] = {}
_lock = threading.Lock()


def _model(key: str, build: Callable[[list, int], Any]) -> Any:
    t = dnn.target(key)
    with _lock:
        m = _cache.get((key, t))
        if m is None:
            m = _cache[(key, t)] = build([str(p) for p in zoo.ensure(key)], t)
        return m


def _run(key: str, build: Callable[[list, int], Any], fn: Callable[[Any], Any]) -> Any:
    """Run on the configured device; if the GPU path fails, once more on the CPU (and stay there)."""
    try:
        return fn(_model(key, build))
    except cv2.error:
        if dnn.target(key) == cv2.dnn.DNN_TARGET_CPU:
            raise
        dnn.mark_cpu_only(key)
        return fn(_model(key, build))


# ---- faces -------------------------------------------------------------------------------------------------------
def _yunet(paths, t):
    return cv2.FaceDetectorYN.create(paths[0], "", (320, 320), 0.6, 0.3, 5000, cv2.dnn.DNN_BACKEND_OPENCV, t)


def faces(img: np.ndarray, min_score: float = 0.6) -> list[dict]:
    """Faces: box, score, and five landmarks (eyes, nose tip, mouth corners)."""
    h, w = img.shape[:2]

    def go(det):
        det.setInputSize((w, h))
        det.setScoreThreshold(float(min_score))
        _, found = det.detect(img)
        return found if found is not None else []
    out = []
    for f in _run("face_detect", _yunet, go):
        x, y, bw, bh = (int(round(v)) for v in f[:4])
        lm = [[round(float(f[4 + 2 * i]), 1), round(float(f[5 + 2 * i]), 1)] for i in range(5)]
        out.append({"box": [x, y, bw, bh], "score": round(float(f[14]), 3), "label": "face",
                    "landmarks": dict(zip(("right_eye", "left_eye", "nose", "mouth_right", "mouth_left"), lm)),
                    "_raw": f})
    return out


def _sface(paths, t):
    return cv2.FaceRecognizerSF.create(paths[0], "", cv2.dnn.DNN_BACKEND_OPENCV, t)


def face_features(img: np.ndarray, face: dict) -> np.ndarray:
    return _run("face_recognize", _sface, lambda rec: rec.feature(rec.alignCrop(img, face["_raw"])))


def same_person(img_a: np.ndarray, img_b: np.ndarray) -> dict:
    """Whether the largest face in each image is the same person (SFace, cosine similarity)."""
    fa, fb = faces(img_a), faces(img_b)
    if not fa or not fb:
        return {"faces_a": len(fa), "faces_b": len(fb), "same_person": None, "note": "a face is needed in both images"}
    big = lambda fs: max(fs, key=lambda f: f["box"][2] * f["box"][3])  # noqa: E731
    ea, eb = face_features(img_a, big(fa)), face_features(img_b, big(fb))
    cos = float(_run("face_recognize", _sface, lambda rec: rec.match(ea, eb, cv2.FaceRecognizerSF_FR_COSINE)))
    return {"faces_a": len(fa), "faces_b": len(fb), "cosine": round(cos, 4), "threshold": FACE_MATCH_COSINE,
            "same_person": cos >= FACE_MATCH_COSINE}


# ---- objects (YOLOX-S, COCO) ------------------------------------------------------------------------------------
class _YoloX:
    SIZE = 640
    STRIDES = (8, 16, 32)

    def __init__(self, path: str, _t: int):
        self.net = dnn.read_net(path, "objects")
        grids, strides = [], []
        for s in self.STRIDES:
            n = self.SIZE // s
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            g = np.stack((xv, yv), 2).reshape(1, -1, 2)
            grids.append(g)
            strides.append(np.full((*g.shape[:2], 1), s))
        self.grids, self.strides = np.concatenate(grids, 1), np.concatenate(strides, 1)

    def detect(self, img: np.ndarray, conf: float, nms: float) -> list[tuple]:
        ratio = min(self.SIZE / img.shape[0], self.SIZE / img.shape[1])
        padded = np.full((self.SIZE, self.SIZE, 3), 114.0, np.float32)
        rs = cv2.resize(img, (int(img.shape[1] * ratio), int(img.shape[0] * ratio)), interpolation=cv2.INTER_LINEAR)
        padded[:rs.shape[0], :rs.shape[1]] = rs
        self.net.setInput(np.transpose(padded, (2, 0, 1))[np.newaxis])
        dets = self.net.forward(self.net.getUnconnectedOutLayersNames())[0][0]
        dets[:, :2] = (dets[:, :2] + self.grids) * self.strides
        dets[:, 2:4] = np.exp(dets[:, 2:4]) * self.strides
        boxes = np.stack([dets[:, 0] - dets[:, 2] / 2, dets[:, 1] - dets[:, 3] / 2, dets[:, 2], dets[:, 3]], 1)
        scores = dets[:, 4:5] * dets[:, 5:]
        best, cls = scores.max(1), scores.argmax(1)
        keep = cv2.dnn.NMSBoxesBatched(boxes.tolist(), best.tolist(), cls.tolist(), conf, nms)
        return [(boxes[i] / ratio, float(best[i]), int(cls[i])) for i in np.array(keep).ravel()]


def objects(img: np.ndarray, min_score: float = 0.35, classes: list[str] | None = None) -> list[dict]:
    """Objects of COCO's 80 classes (people, vehicles, animals, furniture, electronics, food...)."""
    want = {c.lower() for c in classes} if classes else None
    found = _run("objects", lambda p, t: _YoloX(p[0], t), lambda m: m.detect(img, float(min_score), 0.5))
    out = []
    h, w = img.shape[:2]
    for box, score, cls in sorted(found, key=lambda d: -d[1]):
        name = COCO[cls] if cls < len(COCO) else str(cls)
        if want and name not in want:
            continue
        x, y, bw, bh = box
        x0, y0 = max(0, int(x)), max(0, int(y))
        out.append({"label": name, "score": round(score, 3),
                    "box": [x0, y0, int(min(w, x + bw)) - x0, int(min(h, y + bh)) - y0]})
    return out


# ---- text (PP-OCRv3 detection + CRNN recognition) ----------------------------------------------------------------
def _db(paths, _t):
    m = cv2.dnn.TextDetectionModel_DB(dnn.read_net(paths[0], "text_detect"))
    m.setBinaryThreshold(0.3).setPolygonThreshold(0.5).setUnclipRatio(2.0).setMaxCandidates(200)
    return m


def _net(key: str):
    """A builder of `key`'s network (dnn picks its engine and device)."""
    return lambda paths, _t: dnn.read_net(paths[0], key)


_REC_W, _REC_H = 100, 32                     # the recognizer's fixed input
_LINE_H = 48                                 # a text line is straightened to this height before it is split into words


def _straighten(img: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """The text line inside quad (bottom-left, top-left, top-right, bottom-right), straightened, _LINE_H high."""
    bl, tl, tr, br = quad
    lh = max(1.0, (np.linalg.norm(tl - bl) + np.linalg.norm(tr - br)) / 2)
    lw = max(1.0, (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2)
    ow = max(8, int(round(lw * _LINE_H / lh)))
    dst = np.array([[0, _LINE_H - 1], [0, 0], [ow - 1, 0], [ow - 1, _LINE_H - 1]], np.float32)
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(quad, dst), (ow, _LINE_H),
                               flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _ink(line: np.ndarray) -> np.ndarray:
    """Which columns of a straightened line have ink (text dark on light, or light on dark)."""
    gray = cv2.cvtColor(line, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    border = np.concatenate([bw[0], bw[-1], bw[:, 0], bw[:, -1]])
    if np.median(border) > 127:                  # the background is white: ink is black
        bw = 255 - bw
    return (bw > 0).sum(axis=0) > 0


def _runs(mask: np.ndarray, value: bool) -> list[tuple[int, int]]:
    out, start = [], None
    for i, v in enumerate(list(mask) + [not value]):
        if v == value and start is None:
            start = i
        elif v != value and start is not None:
            out.append((start, i))
            start = None
    return out


def _words(line: np.ndarray) -> list[tuple[int, int]]:
    """Column ranges of the words in a straightened line, each trimmed to its ink with a small margin: split at gaps
    clearly wider than this line's own letter spacing."""
    ink = _ink(line)
    inner = [g for g in _runs(ink, False) if g[0] > 0 and g[1] < len(ink)]
    typical = float(np.median([b - a for a, b in inner])) if inner else 0.0
    wide = max(4.0, 0.16 * _LINE_H, 2.2 * typical)
    cuts, prev = [], 0
    for a, b in inner:
        if b - a >= wide:
            cuts.append((prev, a))
            prev = b
    cuts.append((prev, len(ink)))
    out = []
    for a, b in cuts:
        cols = np.flatnonzero(ink[a:b])
        if cols.size:
            out.append((max(0, a + int(cols[0]) - 3), min(len(ink), a + int(cols[-1]) + 4)))
    return out


def _chunks(word: np.ndarray) -> list[np.ndarray]:
    """A word at the recognizer's height, cut into pieces that fit its width without squeezing (at gaps between
    letters where it can), each padded with its own background."""
    h, w = word.shape[:2]
    scaled = cv2.resize(word, (max(1, int(round(w * _REC_H / h))), _REC_H), interpolation=cv2.INTER_AREA)
    sw = scaled.shape[1]
    ink = _ink(scaled) if sw > _REC_W else None
    pieces, start = [], 0
    while sw - start > _REC_W:
        cut = start + int(_REC_W * 0.9)
        free = [i for i in range(start + _REC_W // 2, start + _REC_W) if not ink[i]]
        if free:
            cut = min(free, key=lambda i: abs(i - (start + int(_REC_W * 0.9))))
        pieces.append(scaled[:, start:cut])
        start = cut
    pieces.append(scaled[:, start:])
    out = []
    for piece in pieces:
        pw = piece.shape[1]
        if pw < _REC_W // 3:
            # a short word (one or two letters): pad it to a third of the window first, so stretching it to the
            # window does not distort it beyond recognition
            bg = np.median(np.concatenate([piece[:, 0], piece[:, -1]]), axis=0).astype(np.uint8)
            pad = _REC_W // 3 - pw
            piece = cv2.copyMakeBorder(piece, 0, 0, pad // 2, pad - pad // 2, cv2.BORDER_CONSTANT,
                                       value=[int(x) for x in bg])
        # the recognizer was trained on word crops stretched to its window (as the zoo's demo feeds it); blank
        # padding beside a word makes it read letters that are not there
        out.append(cv2.resize(piece, (_REC_W, _REC_H), interpolation=cv2.INTER_CUBIC))
    return out


def _decode(out: np.ndarray) -> tuple[str, float]:
    """CTC greedy decoding (blank = 0, repeats merged), and the mean probability of the characters kept: text in a
    script the model does not know (Chinese, for this Latin model) comes out as low-confidence junk."""
    chars, probs, prev = [], [], 0
    for i in range(out.shape[0]):
        row = out[i][0].astype(np.float64)
        e = np.exp(row - row.max())
        c = int(np.argmax(row))
        if c != 0 and c != prev:
            chars.append(CHARSET_94[c - 1])
            probs.append(float(e[c] / e.sum()))
        prev = c
    return "".join(chars), (float(np.mean(probs)) if probs else 0.0)


def _clamp_box(quad: np.ndarray, w: int, h: int) -> list[int]:
    x, y, bw, bh = cv2.boundingRect(quad)
    x0, y0 = max(0, int(x)), max(0, int(y))
    return [x0, y0, max(0, min(w, int(x + bw)) - x0), max(0, min(h, int(y + bh)) - y0)]


def text(img: np.ndarray, max_side: int = 1536) -> list[dict]:
    """Text on the image, line by line: the text, its polygon and box, in reading order (top to bottom, left to right).
    The detector runs on the image scaled so its longer side is at most max_side (a multiple of 32)."""
    h, w = img.shape[:2]
    k = min(1.0, max_side / max(h, w))
    iw, ih = max(32, int(round(w * k / 32)) * 32), max(32, int(round(h * k / 32)) * 32)
    small = cv2.resize(img, (iw, ih))

    def det(m):
        m.setInputSize((iw, ih))
        m.setInputMean((123.675, 116.28, 103.53))
        m.setInputScale(1.0 / 255.0 / np.array([0.229, 0.224, 0.225]))
        return m.detect(small)[0]
    polys = _run("text_detect", _db, det)
    sx, sy = w / iw, h / ih
    out = []
    for poly in polys:
        quad = (np.array(poly, np.float32) * [sx, sy]).astype(np.float32)

        line = _straighten(img, quad)
        words, confs = [], []
        for a, b in _words(line):
            parts = []
            for chunk in _chunks(line[:, a:b]):
                def rec(net, chunk=chunk):
                    net.setInput(cv2.dnn.blobFromImage(chunk, size=(_REC_W, _REC_H), mean=127.5, scalefactor=1 / 127.5))
                    return _decode(net.forward())
                t, c = _run("text_recognize", _net("text_recognize"), rec)
                if t:
                    parts.append(t)
                    confs.append((c, len(t)))
            if parts:
                words.append("".join(parts))
        s = " ".join(words)
        if not s:
            continue
        conf = sum(c * n for c, n in confs) / max(1, sum(n for _, n in confs))
        out.append({"text": s, "confidence": round(conf, 3), "box": _clamp_box(quad, w, h),
                    "polygon": [[int(a), int(b)] for a, b in quad]})
    out.sort(key=lambda r: (round((r["box"][1] + r["box"][3] / 2) / max(8, r["box"][3] * 0.6)), r["box"][0]))
    return out


# ---- codes (QR: WeChatQRCode; barcodes: OpenCV's reader) ---------------------------------------------------------
def codes(img: np.ndarray) -> list[dict]:
    """QR codes (WeChat's decoder: its robust finder-pattern search; OpenCV 5 dropped Caffe, so the zoo's CNN
    detector files cannot load, and it runs without them) and barcodes (EAN, UPC...)."""
    out = []
    found, points = cv2.wechat_qrcode_WeChatQRCode().detectAndDecode(img)
    for s, pts in zip(found, points):
        quad = np.array(pts, np.float32).reshape(-1, 2)
        out.append({"kind": "qr", "text": s, "box": _clamp_box(quad, img.shape[1], img.shape[0]),
                    "polygon": [[int(a), int(b)] for a, b in quad]})
    try:
        ok, infos, types, corners = cv2.barcode.BarcodeDetector().detectAndDecodeWithType(img)
        if ok:
            for s, kind, pts in zip(infos, types, corners if corners is not None else []):
                if not s:
                    continue
                quad = np.array(pts, np.float32).reshape(-1, 2)
                out.append({"kind": str(kind).lower() or "barcode", "text": s,
                            "box": _clamp_box(quad, img.shape[1], img.shape[0]),
                            "polygon": [[int(a), int(b)] for a, b in quad]})
    except (cv2.error, AttributeError):
        pass
    return out


# ---- people (PP-HumanSeg) ----------------------------------------------------------------------------------------
def people_mask(img: np.ndarray) -> tuple[dict, np.ndarray]:
    """Which pixels are people: the share of the image and each person-shaped region's box; plus the mask."""
    def go(net):
        rgb = cv2.cvtColor(cv2.resize(img, (192, 192)), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        net.setInput(cv2.dnn.blobFromImage((rgb - 0.5) / 0.5))
        return net.forward()[0]
    out = _run("people", _net("people"), go)
    prob = cv2.resize(out.transpose(1, 2, 0), (img.shape[1], img.shape[0]), interpolation=cv2.INTER_LINEAR)
    mask = (np.argmax(prob, axis=2) == 1).astype(np.uint8) * 255
    found, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    regions = [list(cv2.boundingRect(c)) for c in sorted(found, key=cv2.contourArea, reverse=True)
               if cv2.contourArea(c) > 0.002 * mask.size]
    return {"people_share": round(float((mask > 0).mean()), 4), "regions": [{"box": r, "label": "person"}
                                                                            for r in regions[:50]]}, mask


def public(items: list[dict]) -> list[dict]:
    """Results without their internal fields."""
    return [{k: v for k, v in it.items() if not k.startswith("_")} for it in items]

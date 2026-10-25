"""Every likeness output says it is AI-generated, three ways:

1. A visible label burned into the pixels (every frame of a video): "AI-GENERATED" plus the job id.
2. Embedded metadata: PNG gets an XMP packet (iTXt "XML:com.adobe.xmp") with IPTC's digital source type
   "trainedAlgorithmicMedia" and the manifest; WebM gets the manifest appended as an EBML Void element after the
   segment (players skip it; `read_embedded` finds it).
3. A sidecar manifest, <output>.provenance.json, in the shape of a C2PA manifest (claim generator, actions,
   ingredients, the consent id) with the SHA-256 of the output's pixels/bytes before the metadata was added.

Not yet cryptographically signed C2PA (no signing certificate here); the manifest says so ("signature": null)."""
from __future__ import annotations

import hashlib
import json
import time
import zlib
from pathlib import Path
from typing import Any

import cv2
import numpy as np

LABEL = "AI-GENERATED"
IPTC_SOURCE = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
_VOID_MAGIC = b"ABP-PROVENANCE/1\n"


def label(img: np.ndarray, job_id: str) -> np.ndarray:
    """Draw the visible label in the bottom-left corner, sized to the image, on a dark band so it always reads."""
    h, w = img.shape[:2]
    text = f"{LABEL} | {job_id}"
    scale = max(0.35, min(w, h) / 900)
    th = max(1, int(round(scale * 2)))
    (tw, tht), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, th)
    pad = max(4, int(6 * scale))
    x0, y1 = pad, h - pad
    y0 = max(0, y1 - tht - base - 2 * pad)
    x1 = min(w, x0 + tw + 2 * pad)
    band = img[y0:y1, x0:x1]
    band[:] = (band.astype(np.float32) * 0.35).astype(np.uint8)
    cv2.putText(img, text, (x0 + pad, y1 - base - pad // 2), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), th, cv2.LINE_AA)
    return img


def manifest(*, job_id: str, mode: str, consent: dict, inputs: list[dict], output: Path, content_sha256: str,
             extra: dict | None = None) -> dict:
    return {
        "format": "abp-likeness-provenance/1 (C2PA-shaped, unsigned)",
        "claim_generator": "AgenticBotPlatform likeness",
        "title": output.name,
        "digital_source_type": IPTC_SOURCE,
        "ai_generated": True,
        "visible_label": LABEL,
        "actions": [{"action": "c2pa.created", "digitalSourceType": IPTC_SOURCE, "softwareAgent": "ABP likeness " + mode,
                     "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}],
        "ingredients": inputs,
        "likeness": {"job_id": job_id, "mode": mode, "consent_id": consent.get("id"), "person": consent.get("name"),
                     "consent_kind": consent.get("kind")},
        "content_sha256": content_sha256,
        "signature": None,
        **(extra or {}),
    }


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")


def _xmp(m: dict) -> bytes:
    js = json.dumps(m, separators=(",", ":")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return ("<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>"
            "<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
            "<rdf:Description xmlns:Iptc4xmpExt='http://iptc.org/std/Iptc4xmpExt/2008-02-29/' "
            "xmlns:abp='https://github.com/agenticbotplatform/ns/likeness/1/' "
            f"Iptc4xmpExt:DigitalSourceType='{IPTC_SOURCE}' abp:AIGenerated='True' abp:Manifest=\"{js.replace(chr(34), '&quot;')}\"/>"
            "</rdf:RDF></x:xmpmeta><?xpacket end='w'?>").encode("utf-8")


def write_png(img: np.ndarray, path: Path, m: dict) -> None:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("could not encode the PNG")
    data = buf.tobytes()
    itxt = b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + _xmp(m)
    end = data.rfind(b"IEND") - 4                      # insert before IEND
    path.write_bytes(data[:end] + _png_chunk(b"iTXt", itxt) + data[end:])


def append_webm(path: Path, m: dict) -> None:
    payload = _VOID_MAGIC + json.dumps(m, separators=(",", ":")).encode("utf-8")
    n = len(payload)
    size = (n | (1 << 56)).to_bytes(8, "big")          # EBML 8-byte size vint
    with open(path, "ab") as fh:
        fh.write(b"\xEC" + size + payload)


def read_embedded(path: Path) -> Any:
    """The manifest embedded in a likeness output, or None."""
    data = Path(path).read_bytes()
    i = data.rfind(_VOID_MAGIC)
    if i >= 0:
        return json.loads(data[i + len(_VOID_MAGIC):].decode("utf-8"))
    i = data.find(b"abp:Manifest=\"")
    if i >= 0:
        j = data.index(b'"', i + 14)
        raw = data[i + 14:j].decode("utf-8")
        return json.loads(raw.replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    return None


def write_sidecar(output: Path, m: dict) -> Path:
    p = output.with_name(output.name + ".provenance.json")
    p.write_text(json.dumps(m, indent=1), encoding="utf-8")
    return p


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

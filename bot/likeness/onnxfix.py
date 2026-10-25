"""A tiny ONNX (protobuf) editor with no dependencies: just enough to make InsightFace's inswapper run on OpenCV's DNN.

OpenCV 5's Resize rejects the 3-input form (X, roi, scales) the swapper uses; dropping the empty roi gives the
2-input form (X, scales) it accepts, with the same result. The swapper's last initializer is its "emap" (512x512):
the matrix that maps an ArcFace embedding to the latent the swapper takes, read out here as float32.

Only the fields named below are touched; every other byte is copied through unchanged."""
from __future__ import annotations

import numpy as np

_MODEL_GRAPH, _GRAPH_NODE, _GRAPH_INIT = 7, 1, 5
_NODE_INPUT, _NODE_OP = 1, 4
_T_DIMS, _T_TYPE, _T_INT32, _T_RAW, _T_FLOAT = 1, 2, 5, 9, 4
_FLOAT, _FLOAT16 = 1, 10


def _varint(b, i: int) -> tuple[int, int]:
    v = s = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s
        if c < 0x80:
            return v, i
        s += 7


def _enc_varint(v: int) -> bytes:
    out = bytearray()
    while True:
        c = v & 0x7F
        v >>= 7
        out.append(c | (0x80 if v else 0))
        if not v:
            return bytes(out)


def fields(b) -> list[tuple[int, int, object]]:
    """(field number, wire type, value) of one message; length-delimited values stay memoryviews."""
    b = memoryview(b)
    out, i, n = [], 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wt}")
        out.append((f, wt, v))
    return out


def _encode(fs) -> bytes:
    parts = []
    for f, wt, v in fs:
        parts.append(_enc_varint((f << 3) | wt))
        if wt == 0:
            parts.append(_enc_varint(v))
        elif wt == 2:
            parts.append(_enc_varint(len(v)))
            parts.append(bytes(v))
        else:
            parts.append(bytes(v))
    return b"".join(parts)


def _tensor(b) -> np.ndarray:
    dims, dtype, raw, i32, f32 = [], _FLOAT, None, [], []
    for f, wt, v in fields(b):
        if f == _T_DIMS:
            if wt == 2:
                j, vb = 0, v
                while j < len(vb):
                    d, j = _varint(vb, j)
                    dims.append(d)
            else:
                dims.append(v)
        elif f == _T_TYPE:
            dtype = v
        elif f == _T_RAW:
            raw = bytes(v)
        elif f == _T_INT32 and wt == 2:
            j, vb = 0, v
            while j < len(vb):
                d, j = _varint(vb, j)
                i32.append(d)
        elif f == _T_FLOAT and wt == 2:
            f32 = np.frombuffer(bytes(v), "<f4")
    if dtype == _FLOAT16:
        a = np.frombuffer(raw, "<f2") if raw is not None else np.array(i32, np.uint16).view("<f2")
    elif dtype == _FLOAT:
        a = np.frombuffer(raw, "<f4") if raw is not None else np.asarray(f32, "<f4")
    else:
        raise ValueError(f"unsupported tensor type {dtype}")
    return a.astype(np.float32).reshape(dims)


def patch_inswapper(data: bytes) -> tuple[bytes, np.ndarray, int]:
    """(the model OpenCV can run, the emap as float32, how many Resize nodes were changed)."""
    top = fields(data)
    changed, emap = 0, None
    new_top = []
    for f, wt, v in top:
        if f != _MODEL_GRAPH:
            new_top.append((f, wt, v))
            continue
        graph = []
        last_init = None
        for gf, gwt, gv in fields(v):
            if gf == _GRAPH_NODE:
                nf = fields(gv)
                if any(x == _NODE_OP and bytes(y) == b"Resize" for x, _, y in nf):
                    ins = [y for x, _, y in nf if x == _NODE_INPUT]
                    if len(ins) == 3:
                        keep = {id(ins[1])}
                        nf = [t for t in nf if not (t[0] == _NODE_INPUT and id(t[2]) in keep)]
                        changed += 1
                        gv = _encode(nf)
            elif gf == _GRAPH_INIT:
                last_init = gv
            graph.append((gf, gwt, gv))
        if last_init is not None:
            emap = _tensor(last_init)
        new_top.append((f, wt, _encode(graph)))
    if emap is None or emap.shape != (512, 512):
        raise ValueError("this is not an inswapper model (no 512x512 emap)")
    return _encode(new_top), emap, changed

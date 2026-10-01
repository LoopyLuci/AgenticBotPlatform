"""Read a GGUF file's header: its metadata and tensor list, without loading the weights.

    info = gguf.read(path)
    info["meta"]["general.architecture"]          # "llama", "qwen2", "gemma3", "nomic-bert", ...
    summary(info)                                 # what /api/show and the model list need

Format (GGUF v2/v3, little-endian): magic "GGUF", version, tensor count, metadata count, metadata key/value pairs
(typed), then tensor infos (name, dims, ggml type, offset). Large arrays (the tokenizer's vocabulary) are skipped
unless asked for, so reading a header takes milliseconds.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, BinaryIO

from bot.localai.paths import LocalAIError

_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
_STRING, _ARRAY = 8, 9
# ggml tensor types -> names (the ones that show up in released models)
GGML_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1", 8: "Q8_0", 9: "Q8_1", 10: "Q2_K", 11: "Q3_K",
              12: "Q4_K", 13: "Q5_K", 14: "Q6_K", 15: "Q8_K", 16: "IQ2_XXS", 17: "IQ2_XS", 18: "IQ3_XXS", 19: "IQ1_S",
              20: "IQ4_NL", 21: "IQ3_S", 22: "IQ2_S", 23: "IQ4_XS", 24: "I8", 25: "I16", 26: "I32", 27: "I64", 28: "F64",
              29: "IQ1_M", 30: "BF16", 34: "TQ1_0", 35: "TQ2_0", 39: "MXFP4"}
# general.file_type -> the quantization people call the file by
FILE_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M",
              13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS",
              21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S",
              29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16", 36: "TQ1_0", 37: "TQ2_0", 38: "MXFP4"}


def _str(f: BinaryIO) -> str:
    (n,) = struct.unpack("<Q", f.read(8))
    if n > 1 << 24:
        raise LocalAIError("not a GGUF file (a string is impossibly long)")
    return f.read(n).decode("utf-8", errors="replace")


def _value(f: BinaryIO, t: int, keep_arrays: bool, key: str) -> Any:
    if t in _SCALARS:
        fmt = _SCALARS[t]
        return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]
    if t == _STRING:
        return _str(f)
    if t == _ARRAY:
        (et,) = struct.unpack("<I", f.read(4))
        (n,) = struct.unpack("<Q", f.read(8))
        small = n <= 64 or keep_arrays
        if et in _SCALARS and not small:
            f.seek(struct.calcsize(_SCALARS[et]) * n, 1)
            return {"array": True, "type": et, "length": n}
        out = []
        for _ in range(n):
            v = _value(f, et, keep_arrays, key)
            if small:
                out.append(v)
        return out if small else {"array": True, "type": et, "length": n}
    raise LocalAIError(f"unknown GGUF value type {t} (key {key})")


def read(path: str | Path, keep_arrays: bool = False, tensors: bool = True) -> dict:
    path = Path(path)
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise LocalAIError(f"{path.name} is not a GGUF file")
        (version,) = struct.unpack("<I", f.read(4))
        if version < 2:
            raise LocalAIError(f"{path.name} is GGUF version {version}; only 2 and 3 are read")
        n_tensors, n_kv = struct.unpack("<QQ", f.read(16))
        meta: dict[str, Any] = {}
        for _ in range(n_kv):
            key = _str(f)
            (t,) = struct.unpack("<I", f.read(4))
            meta[key] = _value(f, t, keep_arrays, key)
        info: dict[str, Any] = {"path": str(path), "version": version, "tensor_count": n_tensors, "meta": meta,
                                "file_size": path.stat().st_size}
        if tensors:
            types: dict[str, int] = {}
            params = 0
            for _ in range(n_tensors):
                _name = _str(f)
                (nd,) = struct.unpack("<I", f.read(4))
                dims = struct.unpack(f"<{nd}Q", f.read(8 * nd))
                (tt,) = struct.unpack("<I", f.read(4))
                f.seek(8, 1)
                n = 1
                for d in dims:
                    n *= d
                params += n
                tn = GGML_TYPES.get(tt, str(tt))
                types[tn] = types.get(tn, 0) + n
            info["parameters"] = params
            info["tensor_types"] = types
    return info


def summary(info: dict) -> dict:
    m = info["meta"]
    arch = m.get("general.architecture", "")
    g = lambda k, d=None: m.get(f"{arch}.{k}", d)  # noqa: E731
    params = info.get("parameters") or 0
    ft = m.get("general.file_type")
    quant = FILE_TYPES.get(ft) if isinstance(ft, int) else None
    if not quant and info.get("tensor_types"):
        quant = max(info["tensor_types"].items(), key=lambda kv: kv[1])[0]
    pooling = g("pooling_type")
    is_embed = arch in ("bert", "nomic-bert", "jina-bert-v2", "t5encoder") or pooling not in (None, 0) and not m.get("tokenizer.chat_template")
    return {
        "architecture": arch, "name": m.get("general.name", ""), "parameters": params,
        "parameter_size": _psize(params), "quantization": quant or "unknown", "context_length": g("context_length"),
        "embedding_length": g("embedding_length"), "block_count": g("block_count"), "head_count": g("attention.head_count"),
        "head_count_kv": g("attention.head_count_kv"), "chat_template": m.get("tokenizer.chat_template"),
        "embedding_model": bool(is_embed), "vision_projector": arch == "clip" or "clip.vision.image_size" in m,
        "license": m.get("general.license"), "file_size": info.get("file_size"),
    }


def _psize(n: int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.1f}B".replace(".0B", "B")
    if n >= 1e6:
        return f"{n / 1e6:.0f}M"
    return str(n)


def kv_cache_bytes(s: dict, ctx: int, bytes_per: int = 2) -> int:
    """Memory the KV cache needs at a context length (f16): 2 (K and V) * layers * ctx * kv_heads * head_dim."""
    layers, emb, heads = s.get("block_count") or 0, s.get("embedding_length") or 0, s.get("head_count") or 1
    kv = s.get("head_count_kv") or heads
    head_dim = emb // max(1, heads) if emb else 0
    return 2 * layers * ctx * kv * head_dim * bytes_per

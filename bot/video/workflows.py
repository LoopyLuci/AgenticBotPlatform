"""Versioned workflow templates (bot/video/workflows/*.json), kept in the repo.

A template names its backend, its model (an id in the registry, or "" for none), the VRAM it needs, a frame-count
rule, and typed parameters (string, path, int, number, bool; default, min, max, multiple, max_len, required). A job
stores the exact "<id>@<version>" it ran, so an old asset always says which template made it; changing a template
means a new file with the next version, never an edit of a released one.

`validate` turns a caller's parameters into a complete, typed set (or raises ValueError naming the bad one)."""
from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

DIR = Path(__file__).with_name("workflows")
TYPES = ("string", "path", "int", "number", "bool")
FRAME_RULES = ("any", "4k+1", "8k+1")

_cache: dict[str, dict] = {}


def _check(wf: dict, src: str) -> dict:
    for k in ("id", "version", "backend", "mode", "params"):
        if k not in wf:
            raise ValueError(f"{src}: workflow lacks {k!r}")
    if not isinstance(wf["version"], int) or wf["version"] < 1:
        raise ValueError(f"{src}: version must be a whole number from 1")
    if wf.get("frames_rule", "any") not in FRAME_RULES:
        raise ValueError(f"{src}: frames_rule must be one of {FRAME_RULES}")
    for name, spec in wf["params"].items():
        if spec.get("type") not in TYPES:
            raise ValueError(f"{src}: parameter {name} has an unknown type {spec.get('type')!r}")
    return wf


def load(refresh: bool = False) -> dict[str, dict]:
    """Every template, keyed "<id>@<version>"."""
    if _cache and not refresh:
        return _cache
    out: dict[str, dict] = {}
    for p in sorted(DIR.glob("*.json")):
        wf = _check(json.loads(p.read_text(encoding="utf-8")), p.name)
        key = f"{wf['id']}@{wf['version']}"
        if key in out:
            raise ValueError(f"{p.name}: {key} is defined twice")
        out[key] = wf
    _cache.clear()
    _cache.update(out)
    return _cache


def get(ref: str) -> dict:
    """A template by "<id>@<version>", or by "<id>" (its latest version)."""
    ref = str(ref or "").strip()
    wfs = load()
    if "@" in ref:
        if ref not in wfs:
            raise ValueError(f"no workflow {ref}")
        return wfs[ref]
    found = [w for w in wfs.values() if w["id"] == ref]
    if not found:
        raise ValueError(f"no workflow {ref or '(none given)'}; known: {', '.join(sorted({w['id'] for w in wfs.values()}))}")
    return max(found, key=lambda w: w["version"])


def ref(wf: dict) -> str:
    return f"{wf['id']}@{wf['version']}"


def listing() -> list[dict]:
    return [{"ref": k, "id": w["id"], "version": w["version"], "title": w.get("title", ""), "backend": w["backend"],
             "mode": w["mode"], "model": w.get("model", ""), "vram_gb": w.get("vram_gb", 0),
             "description": w.get("description", ""), "params": w["params"]} for k, w in sorted(load().items())]


def _coerce(name: str, spec: dict, v: Any) -> Any:
    t = spec["type"]
    if t in ("string", "path"):
        if not isinstance(v, str):
            raise ValueError(f"{name} must be text")
        if len(v) > int(spec.get("max_len", 4096)):
            raise ValueError(f"{name} is longer than {spec.get('max_len', 4096)} characters")
        return v
    if t == "bool":
        if not isinstance(v, bool):
            raise ValueError(f"{name} must be true or false")
        return v
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
        raise ValueError(f"{name} must be a number")
    if t == "int":
        if isinstance(v, float) and not v.is_integer():
            raise ValueError(f"{name} must be a whole number")
        v = int(v)
    else:
        v = float(v)
    if "min" in spec and v < spec["min"]:
        raise ValueError(f"{name} must be at least {spec['min']}")
    if "max" in spec and v > spec["max"]:
        raise ValueError(f"{name} must be at most {spec['max']}")
    if t == "int" and spec.get("multiple") and v % int(spec["multiple"]):
        raise ValueError(f"{name} must be a multiple of {spec['multiple']}")
    return v


def validate(wf: dict, params: dict | None) -> dict:
    params = dict(params or {})
    unknown = sorted(set(params) - set(wf["params"]))
    if unknown:
        raise ValueError(f"{ref(wf)} has no parameter {', '.join(unknown)}")
    out: dict[str, Any] = {}
    for name, spec in wf["params"].items():
        if name in params and params[name] is not None:
            out[name] = _coerce(name, spec, params[name])
        elif spec.get("required"):
            raise ValueError(f"{name} is required")
        else:
            out[name] = spec.get("default")
        if spec.get("required") and spec["type"] == "string" and not str(out[name]).strip():
            raise ValueError(f"{name} is required")
    if out.get("seed") == -1:
        out["seed"] = random.randint(0, 2**31 - 1)
    if "seconds" in out and "fps" in out:
        out["frames"] = frames(wf, out["seconds"], out["fps"])
    return out


def frames(wf: dict, seconds: float, fps: int) -> int:
    """The frame count for a clip of `seconds` at `fps`, rounded to the model's rule (Wan 4k+1, LTX 8k+1)."""
    n = max(1, round(seconds * fps))
    rule = wf.get("frames_rule", "any")
    if rule == "any":
        return n
    k = 4 if rule == "4k+1" else 8
    return max(k + 1, round((n - 1) / k) * k + 1)


def render_args(wf: dict, params: dict, files: dict[str, str], output: str) -> list[str]:
    """The backend's command-line arguments: "{name}" is a parameter, "{file:<role>}" a model file, "{output}" the
    output path. An optional argument group is added when its parameter is truthy."""
    def sub(a: str) -> str:
        if a.startswith("{file:") and a.endswith("}"):
            role = a[6:-1]
            if role not in files:
                raise ValueError(f"{ref(wf)} needs a model file for {role}")
            return files[role]
        if a == "{output}":
            return output
        if a.startswith("{") and a.endswith("}"):
            v = params.get(a[1:-1])
            if v is None:
                raise ValueError(f"{ref(wf)}: no value for {a}")
            return str(v)
        return a
    out = [sub(a) for a in wf.get("args", [])]
    for name, group in (wf.get("optional_args") or {}).items():
        if params.get(name):
            out += [sub(a) for a in group]
    return out

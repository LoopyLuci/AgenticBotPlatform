"""Tokens per second for the model picker: measured where we have it, a labelled estimate where we do not.

Measured: every Local Chat reply (and every benchmark) records its generation speed for (model, device), kept as a running
average in data/agent/localchat/speed.json. Estimated: generating one token reads the model's active weights once, so speed is
about (effective memory bandwidth) / (bytes read per token). The effective bandwidth is calibrated from the models already
measured on the same device; with none measured yet a rough figure for the device kind is used and said so.

    record(model, device, tokens_per_s, source)    a measurement ("reply" or "bench")
    figure(row, device, runs_on)                   {"tokens_per_s", "source": measured|estimated|rough_estimate, ...}
"""
from __future__ import annotations

import json
import os
import re
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Optional

_lock = threading.RLock()
ALPHA = 0.3                                  # weight of a new measurement in the running average
ROUGH_GBPS = {"gpu": 300.0, "hybrid": 60.0, "cpu": 30.0}   # effective GB/s before anything was measured on a device


def _path() -> Path:
    from bot.agent_runtime.state import state_dir

    return state_dir("localchat") / "speed.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _key(model: str, device: str) -> str:
    return f"{device or 'cpu'}|{model}"


def record(model: str, device: str, tokens_per_s: float, source: str = "reply", size_gb: float = 0.0,
           active_fraction: float = 1.0) -> None:
    """One measurement. `size_gb`/`active_fraction` let it calibrate estimates for other models on the same device."""
    try:
        tps = float(tokens_per_s)
    except (TypeError, ValueError):
        return
    if not model or tps <= 0 or tps > 100000:
        return
    with _lock:
        data = _load()
        k = _key(model, device)
        cur = data.get(k) or {}
        avg = tps if not cur.get("tokens_per_s") else (1 - ALPHA) * float(cur["tokens_per_s"]) + ALPHA * tps
        data[k] = {"model": model, "device": device or "cpu", "tokens_per_s": round(avg, 2), "last": round(tps, 2),
                   "samples": int(cur.get("samples") or 0) + 1, "source": source, "ts": time.time(),
                   "size_gb": float(size_gb or cur.get("size_gb") or 0), "active_fraction": float(active_fraction or 1.0)}
        _save(data)


def measured(model: str, device: str) -> Optional[dict]:
    return _load().get(_key(model, device))


def active_fraction(row: dict) -> float:
    """The share of the weights read per token: 1 for a dense model; for a mixture of experts the active part, from a size
    label like 30B-A3B (3/30), else from experts used / experts (plus the shared layers, roughly)."""
    label = str(row.get("size_label") or "")
    m = re.match(r"^\s*([\d.]+)\s*B\s*-\s*A\s*([\d.]+)\s*B", label, re.I)
    if m:
        total, active = float(m.group(1)), float(m.group(2))
        if total > 0 and 0 < active <= total:
            return active / total
    experts, used = row.get("experts"), row.get("experts_used")
    try:
        if experts and used and int(experts) > 0:
            return min(1.0, 1.5 * int(used) / int(experts))
    except (TypeError, ValueError):
        pass
    return 1.0


def bytes_per_token_gb(row: dict) -> float:
    try:
        size = float(row.get("size_gb") or 0)
    except (TypeError, ValueError):
        size = 0.0
    return size * active_fraction(row)


def _calibration(device: str) -> Optional[float]:
    """Effective GB/s on `device`: the median over the models measured there."""
    vals = []
    for v in _load().values():
        if v.get("device") == (device or "cpu") and v.get("size_gb") and v.get("tokens_per_s"):
            vals.append(float(v["tokens_per_s"]) * float(v["size_gb"]) * float(v.get("active_fraction") or 1.0))
    return statistics.median(vals) if vals else None


def figure(row: dict, device: str, runs_on: str) -> dict[str, Any]:
    """{"tokens_per_s": float|None, "source": "measured"|"estimated"|"rough_estimate"|"unknown", "samples", "note"}."""
    m = measured(str(row.get("id") or ""), device)
    if m and m.get("tokens_per_s"):
        return {"tokens_per_s": m["tokens_per_s"], "source": "measured", "samples": m.get("samples", 1),
                "note": f"average of {m.get('samples', 1)} {'benchmark' if m.get('source') == 'bench' else 'reply'} measurement(s) here"}
    bpt = bytes_per_token_gb(row)
    if bpt <= 0 or runs_on == "none":
        return {"tokens_per_s": None, "source": "unknown", "samples": 0, "note": "no size to estimate from" if bpt <= 0 else "does not fit here"}
    cal = _calibration(device) if runs_on == "gpu" else None
    if cal:
        return {"tokens_per_s": round(cal / bpt, 1), "source": "estimated", "samples": 0,
                "note": "from this device's measured models (memory bandwidth / weights read per token)"}
    gbps = ROUGH_GBPS.get(runs_on, ROUGH_GBPS["cpu"])
    return {"tokens_per_s": round(gbps / bpt, 1), "source": "rough_estimate", "samples": 0,
            "note": f"rough: nothing measured on this device yet (assumes ~{gbps:.0f} GB/s effective for {runs_on})"}

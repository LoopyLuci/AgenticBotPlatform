"""The load governor: from one health reading and the WHEA summary, how much heavy work may run now.

    decide(snap, whea, cfg) -> {"level": "ok"|"caution"|"pause", "reasons": [...], "caps": {kind: n}, "heavy_allowed": bool}
    verdict(max_age_s=60)   -> the latest verdict: the monitor's shared file when fresh, else a reading taken now
    cap(kind, want)         -> `want` limited by the verdict (0 = do not start); what pytest -n, the conductor, Factory runs use

Levels:
  pause    an unacknowledged WHEA event, or a reading over a pause limit (temperature, free RAM, commit, a watched disk)
           -> every cap 0: new heavy work waits, guarded running work is suspended (bot/hwguard/__main__.py watch)
  caution  a reading over a caution limit, sustained CPU load, or a fatal WHEA event inside `whea_caution_days`
           -> caps halved (at least 1)
  ok       the configured caps
A kind whose `govern.<kind>` switch is off gets its configured cap whatever the level; `enabled: false` turns the whole
governor off (caps as configured, no slot). Settings live under `hwguard:` in config/backends.yaml.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

KINDS = ("pytest", "factory", "render", "model_load", "build")
OK, CAUTION, PAUSE = "ok", "caution", "pause"
DEFAULTS: dict[str, Any] = {
    "enabled": True,              # the governor as a whole
    "monitor": True,              # the sampling thread in ABP
    "whea_watch": True,           # read the WHEA log; a new event pauses heavy work
    "alerts": True,               # alerts through the Sentinel journal and push
    "exclusive": True,            # one heavy job at a time (the shared slot)
    "interval_s": 15,
    "govern": {k: True for k in KINDS},
    "caps": {"pytest": 4, "factory": 2, "render": 1, "model_load": 1, "build": 2},
    "cpu_temp_caution_c": 80.0, "cpu_temp_pause_c": 90.0,
    "gpu_temp_caution_c": 85.0, "gpu_temp_pause_c": 95.0,
    "cpu_load_caution_pct": 90.0,
    "vram_caution_pct": 92.0,
    "ram_caution_gb": 6.0, "ram_pause_gb": 2.0,
    "commit_caution_pct": 85.0, "commit_pause_pct": 95.0,
    "disk_pause_gb": 5.0, "disks": [],
    "whea_caution_days": 7.0,
}
_NESTED = ("govern", "caps")
NUMBERS = tuple(k for k, v in DEFAULTS.items() if isinstance(v, float) or k == "interval_s")
SWITCHES = ("enabled", "monitor", "whea_watch", "alerts", "exclusive")


def settings() -> dict[str, Any]:
    out = {**DEFAULTS, **{k: dict(DEFAULTS[k]) for k in _NESTED}}
    try:
        from bot.config import config

        got = config.current.get("hwguard") or {}
    except Exception:  # noqa: BLE001
        got = {}
    if isinstance(got, dict):
        for k, v in got.items():
            if k in _NESTED and isinstance(v, dict):
                out[k].update({kk: vv for kk, vv in v.items() if kk in KINDS})
            elif k in DEFAULTS:
                out[k] = v
    if os.environ.get("ABP_HWGUARD", "").strip().lower() in ("0", "off", "false"):
        out["enabled"] = out["monitor"] = False   # the test suite's default (tests/conftest.py); never set on a real machine
    return out


def validate(values: dict) -> dict:
    """The settings change `values` as stored: unknown keys and bad types raise ValueError."""
    clean: dict[str, Any] = {}
    for k, v in values.items():
        if k in SWITCHES:
            if not isinstance(v, bool):
                raise ValueError(f"{k} is true or false")
            clean[k] = v
        elif k in NUMBERS:
            try:
                clean[k] = float(v) if k != "interval_s" else max(5, int(v))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{k} is a number") from exc
            if clean[k] < 0:
                raise ValueError(f"{k} is not negative")
        elif k == "govern" and isinstance(v, dict):
            clean[k] = {kk: bool(vv) for kk, vv in v.items() if kk in KINDS}
        elif k == "caps" and isinstance(v, dict):
            clean[k] = {kk: max(0, int(vv)) for kk, vv in v.items() if kk in KINDS}
        elif k == "disks" and isinstance(v, list):
            clean[k] = [str(x)[:260] for x in v][:16]
        else:
            raise ValueError(f"unknown hardware-guard setting: {k}")
    return clean


def decide(snap: dict, whea: Optional[dict], cfg: dict) -> dict:
    """Pure: the level, why, and the cap for every kind."""
    pause: list[str] = []
    caution: list[str] = []

    def over(val, lim, label, unit, bucket):
        if isinstance(val, (int, float)) and isinstance(lim, (int, float)) and val >= lim:
            bucket.append(f"{label} {val:g}{unit} (limit {lim:g}{unit})")
            return True
        return False

    cpu, gpu, mem = snap.get("cpu") or {}, snap.get("gpu") or {}, snap.get("memory") or {}
    if not over(cpu.get("temp_c"), cfg["cpu_temp_pause_c"], "CPU temperature", " C", pause):
        over(cpu.get("temp_c"), cfg["cpu_temp_caution_c"], "CPU temperature", " C", caution)
    if not over(gpu.get("temp_c"), cfg["gpu_temp_pause_c"], "GPU temperature", " C", pause):
        over(gpu.get("temp_c"), cfg["gpu_temp_caution_c"], "GPU temperature", " C", caution)
    over(cpu.get("load_pct"), cfg["cpu_load_caution_pct"], "CPU load", "%", caution)
    over(gpu.get("vram_pct"), cfg["vram_caution_pct"], "VRAM in use", "%", caution)
    avail = mem.get("available_gb")
    if isinstance(avail, (int, float)):
        if avail <= cfg["ram_pause_gb"]:
            pause.append(f"free RAM {avail:g} GB (limit {cfg['ram_pause_gb']:g} GB)")
        elif avail <= cfg["ram_caution_gb"]:
            caution.append(f"free RAM {avail:g} GB (limit {cfg['ram_caution_gb']:g} GB)")
    commit = (mem.get("commit") or {}).get("pct")
    if not over(commit, cfg["commit_pause_pct"], "memory commit", "%", pause):
        over(commit, cfg["commit_caution_pct"], "memory commit", "%", caution)
    watched = {str(d).rstrip("\\/").lower() for d in cfg.get("disks") or []}
    for d in snap.get("disks") or []:
        if watched and str(d.get("mount", "")).rstrip("\\/").lower() not in watched:
            continue
        if isinstance(d.get("free_gb"), (int, float)) and d["free_gb"] <= cfg["disk_pause_gb"]:
            pause.append(f"disk {d['mount']} has {d['free_gb']:g} GB free (limit {cfg['disk_pause_gb']:g} GB)")
    if whea and cfg.get("whea_watch", True):
        new = whea.get("new") or []
        if new:
            e = new[0]
            pause.append(f"{len(new)} new hardware error(s) in the WHEA log, latest {e.get('kind')} at {e.get('time')} "
                         "- acknowledge it on the Hardware screen to resume")
        if whea.get("recent_fatal"):
            caution.append(f"{whea['recent_fatal']} fatal hardware error(s) in the last {cfg['whea_caution_days']:g} days")
    level = PAUSE if pause else CAUTION if caution else OK
    if not cfg.get("enabled", True):
        level = OK
    caps = {}
    for kind in KINDS:
        base = int(cfg["caps"].get(kind, 1))
        if kind == "pytest":
            base = min(base, max(1, int((cpu.get("cores") or 2) // 2)))
        if not cfg["govern"].get(kind, True) or level == OK:
            caps[kind] = base
        elif level == CAUTION:
            caps[kind] = max(1, base // 2) if base else 0
        else:
            caps[kind] = 0
    return {"level": level, "reasons": pause + caution if cfg.get("enabled", True) else ["the governor is switched off"],
            "caps": caps, "heavy_allowed": level != PAUSE, "enabled": bool(cfg.get("enabled", True))}


# ---- the shared folder: the verdict file, the WHEA acknowledgement, the slot -----------------------------------------
def shared_dir() -> Path:
    """Machine-wide, so the pipeline, the render harness and every ABP checkout see one state: ABP_HWGUARD_DIR, else the
    system temp folder."""
    p = Path(os.environ.get("ABP_HWGUARD_DIR") or (Path(tempfile.gettempdir()) / "abp-hwguard"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read_json(path: Path) -> Optional[dict]:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
    os.replace(tmp, path)


def ack_ts() -> float:
    """When WHEA events were last acknowledged. The first time the guard runs, now: older events are history (they
    still count toward caution), only later ones pause."""
    path = shared_dir() / "whea_ack.json"
    got = _read_json(path)
    if got and isinstance(got.get("ts"), (int, float)):
        return float(got["ts"])
    now = time.time()
    write_json(path, {"ts": now, "by": "first start"})
    return now


def acknowledge(by: str = "dashboard", note: str = "") -> dict:
    data = {"ts": time.time(), "by": by[:60], "note": note[:300]}
    write_json(shared_dir() / "whea_ack.json", data)
    return data


def evaluate(snap: Optional[dict] = None, events: Optional[list] = None, cfg: Optional[dict] = None) -> dict:
    from bot.hwguard import sensors, whea

    cfg = cfg or settings()
    snap = snap if snap is not None else sensors.snapshot(cfg.get("disks") or None)
    if events is None:
        events = whea.read() if cfg.get("whea_watch", True) else []
    summary = whea.summarize(events, ack_ts(), time.time(), float(cfg["whea_caution_days"]))
    return {"ts": time.time(), "snapshot": snap, "whea": summary, "events": events[:50], "verdict": decide(snap, summary, cfg)}


def verdict(max_age_s: float = 60.0) -> dict:
    """The newest verdict: the monitor's file when it is fresh, else measured now (and written for the next caller)."""
    path = shared_dir() / "state.json"
    got = _read_json(path)
    if got and time.time() - float(got.get("ts") or 0) <= max_age_s and isinstance(got.get("verdict"), dict):
        return got["verdict"]
    state = evaluate()
    try:
        write_json(path, state)
    except OSError:
        pass
    return state["verdict"]


def cap(kind: str, want: int) -> int:
    """`want` workers of `kind`, limited by the verdict. Never raises: an unreadable guard leaves `want` as it is."""
    try:
        cfg = settings()
        if not cfg.get("enabled", True) or not cfg["govern"].get(kind, True):
            return int(want)
        v = verdict()
        return max(0, min(int(want), int(v["caps"].get(kind, want))))
    except Exception:  # noqa: BLE001
        return int(want)

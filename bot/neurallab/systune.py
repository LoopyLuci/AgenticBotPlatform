"""System models: small, hyper-specific networks trained on this machine's own measurements, each steering one of
ABP's knobs. They learn on the GPU (the Neural Lab) and decide on the CPU in microseconds (bot/neurallab/infer.py).

    transfer   drive throughput by drive kind, operation, block size and parallelism, measured with unbuffered I/O
               (bench_drive) and from real copies -> the block size and parallel files for each copy the file server's
               transfer engine makes (advise_copy)
    llm        llama.cpp speed (prompt and generation tokens/s) by model size, quantization, batch, micro-batch and
               flash attention, measured with llama-bench on the GPU -> the options ABP's model server starts a model
               with (advise_llm)
    memory     RAM use five minutes ahead from the last minutes of telemetry -> unload idle models before memory runs
               short (forecast_memory)
    stability  the CPU's machine-check history (cores that failed) plus an autoencoder of this machine's normal load,
               clock and memory patterns -> which processors ABP's heavy processes may use and how many threads
               (cpu_policy, guard_process)

Every model is a mixture of experts (spec.moe_regressor): each expert specialises in a regime (a kind of drive, a
model size, a load level). Decisions only touch ABP's own work: block sizes, parallelism, model options, unloading,
and the processor affinity of ABP's own child processes. No system setting changes.
"""
from __future__ import annotations

import ctypes
import json
import math
import mmap
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import psutil

from bot.localai.paths import CPU_THREADS, LocalAIError
from bot.neurallab import lab, spec as specs, telemetry

WIN = sys.platform == "win32"
_NO_WINDOW = 0x08000000 if WIN else 0
Log = Callable[[str], None]
KINDS = ("transfer", "llm", "memory", "stability")
CHUNKS_KB = (64, 256, 1024, 4096, 16384)
_cache: dict = {}
_lock = threading.Lock()


def home() -> Path:
    p = lab.root() / "systune"
    p.mkdir(parents=True, exist_ok=True)
    return p


def state() -> dict:
    try:
        return json.loads((home() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    tmp = home() / "state.json.tmp"
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    os.replace(tmp, home() / "state.json")


def model(kind: str):
    """The adopted model for a kind (cached; reloaded when a newer one is adopted), or None."""
    from bot.neurallab.infer import Model
    st = state().get(kind) or {}
    exp = st.get("export")
    if not exp:
        return None
    with _lock:
        hit = _cache.get(kind)
        if hit and hit[0] == exp:
            return hit[1]
        try:
            m = Model.load(exp)
        except LocalAIError:
            return None
        _cache[kind] = (exp, m)
        return m


# ---- drives ---------------------------------------------------------------------------------------------------------- #

def drives(refresh: bool = False, cached_only: bool = False) -> dict[str, dict]:
    """Volume letter (or mount point) -> {media, bus, device, free}: from the file server's disk inventory, kept in
    <systune>/drives.json for a day (the inventory asks the OS, which takes seconds: never on a copy's path)."""
    now = time.time()
    if not refresh and _cache.get("drives") and now - _cache["drives"][0] < 3600:
        return _cache["drives"][1]
    f = home() / "drives.json"
    if not refresh:
        try:
            saved = json.loads(f.read_text(encoding="utf-8"))
            if cached_only or now - saved["t"] < 86400:
                _cache["drives"] = (now, saved["drives"])
                return saved["drives"]
        except (OSError, ValueError, KeyError):
            if cached_only:
                return {}
    from bot.fileserver import disks
    inv = disks.inventory(record=False)
    out = {}
    for d in inv.get("drives", []):
        for v in d.get("volumes") or []:
            key = v.rstrip("\\/").upper() if WIN else v
            try:
                free = psutil.disk_usage(key + ("\\" if WIN else "")).free
            except OSError:
                free = 0
            out[key] = {"media": (d.get("media") or ("HDD" if d.get("rotational") else "SSD") or "").upper(),
                        "bus": str(d.get("bus") or "").upper(), "device": d.get("device"), "model": d.get("model"), "free": free,
                        "block_cache": block_cache() is not None}
    _cache["drives"] = (now, out)
    try:
        f.write_text(json.dumps({"t": now, "drives": out}), encoding="utf-8")
    except OSError:
        pass
    return out


def drive_of(path: str | Path) -> str:
    p = Path(path).resolve()
    if WIN:
        return p.drive.upper()
    best = "/"
    for part in psutil.disk_partitions(all=False):
        if str(p).startswith(part.mountpoint) and len(part.mountpoint) > len(best):
            best = part.mountpoint
    return best


_MEDIA = ("HDD", "SSD", "SCM")
_BUS = ("NVME", "SATA", "USB", "RAID", "SAS")


def _drive_features(info: dict) -> dict:
    media, bus = info.get("media", ""), info.get("bus", "")
    f = {f"media_{m.lower()}": float(media == m) for m in _MEDIA}
    f.update({f"bus_{b.lower()}": float(b in bus) for b in _BUS})
    f["network"] = float(info.get("network", False))
    f["block_cache"] = float(info.get("block_cache", block_cache() is not None))
    return f


def block_cache() -> Optional[str]:
    """A block-level cache under the file system (PrimoCache, Intel RST/Optane caching, AMD StoreMI...): it serves
    recently used blocks from RAM or an SSD, so measured speeds include it, which is what copies see too."""
    if "block_cache" in _cache:
        return _cache["block_cache"]
    found = None
    if WIN:
        try:
            r = subprocess.run(["sc", "query", "type=", "driver"], capture_output=True, text=True, timeout=30,
                               creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
            names = {"FancyCcV": "PrimoCache", "iaStorAfs": "Intel Optane caching", "rcbottom": "AMD StoreMI",
                     "FuzeDrive": "FuzeDrive", "EnmotusFuzeDrive": "FuzeDrive"}
            for drv, label in names.items():
                if re.search(rf"SERVICE_NAME:\s*{drv}\b", r.stdout, re.I):
                    found = label
                    break
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        if any(Path("/sys/block").glob("bcache*")):
            found = "bcache"
    _cache["block_cache"] = found
    return found


# ---- transfer: measuring ---------------------------------------------------------------------------------------------- #

class _Direct:
    """Unbuffered file I/O (the device's own speed, not the page cache's): FILE_FLAG_NO_BUFFERING on Windows,
    O_DIRECT on Linux. Buffers are page-aligned (mmap); sizes are multiples of the block."""

    def __init__(self, path: Path, write: bool):
        self.path = path
        if WIN:
            k = ctypes.windll.kernel32
            k.CreateFileW.restype = ctypes.c_void_p
            k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.c_uint32, ctypes.c_void_p]
            k.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
            k.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            # NO_BUFFERING (no page cache), sequential reads; writes end with one flush (close), as a durable copy does,
            # not write-through on every block (which no copy uses, and which costs a device flush per block)
            flags = 0x20000000 | (0 if write else 0x08000000)
            k.FlushFileBuffers.argtypes = [ctypes.c_void_p]
            h = k.CreateFileW(str(path), 0x40000000 if write else 0x80000000, 0, None, 2 if write else 3, flags, None)
            if h in (None, ctypes.c_void_p(-1).value):
                raise OSError(ctypes.get_last_error() or ctypes.GetLastError(), f"can't open {path} unbuffered")
            self.h, self.k = h, k
        else:
            self.fd = os.open(path, (os.O_WRONLY | os.O_CREAT | os.O_TRUNC if write else os.O_RDONLY) | getattr(os, "O_DIRECT", 0), 0o600)

    def write(self, buf: mmap.mmap, n: int) -> None:
        if WIN:
            done = ctypes.c_uint32(0)
            addr = ctypes.addressof(ctypes.c_char.from_buffer(buf))
            if not self.k.WriteFile(self.h, addr, n, ctypes.byref(done), None) or done.value != n:
                raise OSError(f"write to {self.path} failed")
        else:
            os.write(self.fd, memoryview(buf)[:n])

    def read(self, buf: mmap.mmap, n: int) -> int:
        if WIN:
            done = ctypes.c_uint32(0)
            addr = ctypes.addressof(ctypes.c_char.from_buffer(buf))
            if not self.k.ReadFile(self.h, addr, n, ctypes.byref(done), None):
                raise OSError(f"read from {self.path} failed")
            return done.value
        return os.readv(self.fd, [memoryview(buf)[:n]])

    def close(self, flush: bool = False) -> None:
        if WIN:
            if flush:
                self.k.FlushFileBuffers(self.h)
            self.k.CloseHandle(self.h)
        else:
            if flush:
                os.fsync(self.fd)
            os.close(self.fd)


def _io_run(files: list[Path], chunk: int, per_file: int, write: bool) -> float:
    """Each file on its own thread (parallel copies), unbuffered; returns MB/s over all of them."""
    errs: list[BaseException] = []

    def one(p: Path):
        buf = mmap.mmap(-1, chunk)                       # page-aligned, as unbuffered I/O requires
        if write:
            buf[:] = os.urandom(chunk)                   # incompressible (some SSDs compress)
        try:
            f = _Direct(p, write)
            try:
                left = per_file
                while left > 0:
                    if write:
                        f.write(buf, chunk)
                    else:
                        if f.read(buf, chunk) <= 0:
                            break
                    left -= chunk
            finally:
                f.close(flush=write)                     # inside the timing: the data is on the device when it ends
        except BaseException as e:                       # noqa: BLE001 - reported below
            errs.append(e)
        finally:
            buf.close()
    t0 = time.perf_counter()
    ths = [threading.Thread(target=one, args=(p,)) for p in files]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    dt = time.perf_counter() - t0
    if errs:
        raise errs[0]
    return len(files) * per_file / dt / 2**20


def bench_drive(path: str | Path, size_mb: int = 128, chunks_kb=CHUNKS_KB, threads=(1, 4), log: Log = lambda m: None) -> list[dict]:
    """Measure one drive: write then read `size_mb` (split over the parallel files) for every block size and
    parallelism, unbuffered. About 2.5 GB of I/O at the defaults, a few seconds on an SSD, ~30 s on a hard disk.
    Uses a temporary folder on that drive, removed afterwards; needs size_mb * 2 of free space."""
    base = Path(path)
    drv = drive_of(base)
    info = drives().get(drv, {})
    free = psutil.disk_usage(str(base)).free
    if free < size_mb * 2 << 20:
        raise LocalAIError(f"{drv} has {free >> 20} MB free; the test needs {size_mb * 2} MB")
    tmp = base / f".abp-iobench-{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    rows = []
    try:
        for th in threads:
            for ck in chunks_kb:
                chunk = ck << 10
                per_file = max(chunk, (size_mb << 20) // th // chunk * chunk)
                files = [tmp / f"t{i}.bin" for i in range(th)]
                busy = _disk_busy()
                w = _io_run(files, chunk, per_file, True)
                r = _io_run(files, chunk, per_file, False)
                for op, v in (("write", w), ("read", r)):
                    rows.append({"drive": drv, **{k: info.get(k) for k in ("media", "bus", "model", "block_cache")}, "op": op, "chunk_kb": ck,
                                 "threads": th, "size_mb": size_mb, "mb_s": round(v, 1), "busy_before": busy, "t": time.time()})
                log(f"{drv} {th} file(s) x {ck} KB blocks: write {w:,.0f} MB/s, read {r:,.0f} MB/s")
                for f in files:
                    f.unlink(missing_ok=True)
    finally:
        for f in tmp.glob("*"):
            f.unlink(missing_ok=True)
        tmp.rmdir()
    for r in rows:
        telemetry.log_event("io_bench", r)
    return rows


def _disk_busy() -> float:
    s = telemetry.history(60)
    if not s:
        return 0.0
    return max((d.get("busy", 0) for d in s[-1].get("disks", {}).values()), default=0.0)


def bench_all(log: Log = lambda m: None, size_mb: int = 128) -> list[dict]:
    """Every local drive with room for the test (ABP's own temp folder on each)."""
    rows = []
    for drv, info in sorted(drives(refresh=True).items()):
        if info.get("free", 0) < (size_mb * 4) << 20:
            log(f"{drv}: skipped ({info.get('free', 0) >> 20} MB free)")
            continue
        try:
            rows += bench_drive(drv + ("\\" if WIN else ""), size_mb, log=log)
        except (OSError, LocalAIError) as e:
            log(f"{drv}: {e}")
    return rows


def record_copy(src: str, dst: str, nbytes: int, seconds: float, chunk: int, parallel: int) -> None:
    """A real copy the transfer engine made: more training data (the read side of src, the write side of dst). Only
    copies larger than half the free memory: smaller ones mostly measure the page cache, not the drives."""
    if seconds <= 0 or nbytes < max(16 << 20, psutil.virtual_memory().available // 2):
        return
    dv = drives(cached_only=True)
    for drv, op in ((drive_of(src), "read"), (drive_of(dst), "write")):
        info = dv.get(drv, {})
        telemetry.log_event("io_copy", {"drive": drv, "media": info.get("media"), "bus": info.get("bus"), "op": op,
                                        "chunk_kb": chunk >> 10, "threads": parallel, "size_mb": nbytes >> 20,
                                        "mb_s": round(nbytes / seconds / 2**20, 1), "busy_before": 0.0})


def _transfer_row(r: dict) -> dict:
    return {**_drive_features(r), "op_write": float(r["op"] == "write"), "log_chunk": math.log2(max(4, r["chunk_kb"])),
            "threads": float(r["threads"]), "log_size": math.log2(max(1, r["size_mb"])),
            "busy": float(r.get("busy_before") or 0) / 100}


# ---- llm: measuring ----------------------------------------------------------------------------------------------------- #

def bench_llm(name: str, log: Log = lambda m: None, batches=(512, 2048), ubatches=(128, 512), fa=("on", "off"),
              n_prompt: int = 512, n_gen: int = 128) -> list[dict]:
    """llama-bench on the GPU for one model over batch x micro-batch x flash attention (CPU threads capped)."""
    from bot.localai import engine, gguf, models
    rec = models.resolve(name)
    s = gguf.summary(gguf.read(rec["weights"], tensors=True))
    if s.get("embedding_model"):
        raise LocalAIError(f"{name} is an embedding model (no generation to measure)")
    argv = [engine.tool("llama-bench"), "-m", rec["weights"], "-ngl", "999", "-t", str(CPU_THREADS), "-r", "2",
            "-p", str(n_prompt), "-n", str(n_gen), "-b", ",".join(map(str, batches)), "-ub", ",".join(map(str, ubatches)),
            "-fa", ",".join(fa), "-o", "json"]
    log(f"llama-bench {models.canonical(name)}: {len(batches) * len(ubatches) * len(fa)} settings")
    r = subprocess.run(argv, capture_output=True, text=True, timeout=1800, creationflags=_NO_WINDOW | 0x00004000 if WIN else 0,
                       encoding="utf-8", errors="replace")
    if r.returncode:
        raise LocalAIError(f"llama-bench failed: {(r.stderr or r.stdout)[-800:]}")
    out = json.loads(r.stdout[r.stdout.index("["):])
    size = os.path.getsize(rec["weights"])
    rows = []
    for x in out:
        params = int(x.get("model_n_params") or 0) or 1
        rows.append({"model": models.canonical(name), "params_b": params / 1e9, "size_gb": size / 2**30,
                     "bits": size * 8 / params, "n_batch": int(x["n_batch"]), "n_ubatch": int(x["n_ubatch"]),
                     "flash_attn": float(x.get("flash_attn") in (1, True, "on", "1")),
                     "test": "gen" if int(x.get("n_gen") or 0) else "prompt", "n_tokens": int(x.get("n_gen") or x.get("n_prompt")),
                     "tok_s": float(x["avg_ts"]), "layers": s.get("block_count") or 0, "t": time.time()})
    for row in rows:
        telemetry.log_event("llm_bench", row)
        log(f"  b={row['n_batch']} ub={row['n_ubatch']} fa={int(row['flash_attn'])} {row['test']}: {row['tok_s']:,.1f} tok/s")
    return rows


def _llm_row(r: dict) -> dict:
    return {"log_params": math.log2(max(0.01, r["params_b"])), "bits": r["bits"], "log_size": math.log2(max(0.01, r["size_gb"])),
            "log_batch": math.log2(r["n_batch"]), "log_ubatch": math.log2(r["n_ubatch"]), "flash_attn": r["flash_attn"],
            "gen": float(r["test"] == "gen"), "log_tokens": math.log2(max(1, r["n_tokens"]))}


# ---- memory and stability: features from telemetry ----------------------------------------------------------------------- #

def _ccd_loads(per: list[float]) -> tuple[float, float]:
    topo = telemetry.topology()
    groups: dict = {}
    for r, v in zip(topo, per):
        groups.setdefault(r["ccd"] if r["ccd"] is not None else r["logical"] * 2 // max(1, len(per)), []).append(v)
    vals = [sum(g) / len(g) for _, g in sorted(groups.items())] + [0.0, 0.0]
    return vals[0], vals[1]


def _stab_row(s: dict) -> dict:
    per = s.get("per_cpu") or [s.get("cpu", 0)]
    c0, c1 = _ccd_loads(per)
    return {"cpu": s.get("cpu", 0) / 100, "cpu_max": s.get("cpu_max", 0) / 100, "hot_cores": sum(v > 90 for v in per) / len(per),
            "ccd0": c0 / 100, "ccd1": c1 / 100, "freq": (s.get("freq_mhz") or 0) / 5000, "ram": s.get("ram_used", 0) / 100,
            "gpu": (s.get("gpu_compute") or s.get("gpu_3d") or 0) / 100, "abp": s.get("abp_cpu", 0) / 100}


def _mem_rows(hist: list[dict], ahead_s: float = 300) -> tuple[list[dict], list[float]]:
    rows, ys = [], []
    ts = np.array([h["t"] for h in hist])
    ram = np.array([h["ram_used"] for h in hist], dtype=float)
    for i in range(len(hist)):
        t = ts[i]
        j = np.searchsorted(ts, t + ahead_s)
        if j >= len(hist) or abs(ts[j] - (t + ahead_s)) > 30:
            continue
        rows.append(_mem_features(hist, i, ts, ram))
        ys.append(float(ram[j]))
    return rows, ys


def _mem_features(hist, i, ts, ram) -> dict:
    t = ts[i]
    w2 = ram[(ts > t - 120) & (ts <= t)]
    w5 = (ts > t - 300) & (ts <= t)
    slope = float(np.polyfit(ts[w5] - t, ram[w5], 1)[0] * 60) if w5.sum() >= 3 else 0.0
    h = hist[i]
    hour = time.localtime(t).tm_hour + time.localtime(t).tm_min / 60
    return {"ram": float(ram[i]), "ram_2m": float(w2.mean()) if len(w2) else float(ram[i]), "slope_per_min": slope,
            "swap": float(h.get("swap_used", 0)), "gpu_mem": float(h.get("gpu_mem_gb") or 0), "cpu": float(h.get("cpu", 0)),
            "abp_cpu": float(h.get("abp_cpu", 0)), "hour_sin": math.sin(2 * math.pi * hour / 24), "hour_cos": math.cos(2 * math.pi * hour / 24)}


# ---- datasets and training ------------------------------------------------------------------------------------------------ #

MIN_ROWS = {"transfer": 40, "llm": 16, "memory": 600, "stability": 1500}


def dataset(kind: str) -> tuple[list[dict], list]:
    """(feature rows, targets) for a kind, from what has been measured so far."""
    if kind == "transfer":
        src = telemetry.events("io_bench", 365 * 86400) + telemetry.events("io_copy", 365 * 86400)
        return [_transfer_row(r) for r in src], [math.log2(max(0.1, r["mb_s"])) for r in src]
    if kind == "llm":
        src = telemetry.events("llm_bench", 365 * 86400)
        return [_llm_row(r) for r in src], [math.log2(max(0.01, r["tok_s"])) for r in src]
    if kind == "memory":
        return _mem_rows(telemetry.history(14 * 86400))
    if kind == "stability":
        rows = [_stab_row(s) for s in telemetry.history(14 * 86400)]
        return rows, [None] * len(rows)
    raise LocalAIError(f"unknown system model {kind}; one of {', '.join(KINDS)}")


def design(kind: str, features: int) -> dict:
    if kind == "stability":                        # an autoencoder: normal patterns reconstruct well, unusual ones don't
        return {"name": "systune-stability", "task": "anomaly", "input": {"kind": "features", "size": features},
                "nodes": [{"id": "enc", "op": "linear", "in": "input", "params": {"out": 16}},
                          {"id": "a1", "op": "gelu", "in": "enc"},
                          {"id": "code", "op": "linear", "in": "a1", "params": {"out": 3}},
                          {"id": "a2", "op": "gelu", "in": "code"},
                          {"id": "dec", "op": "moe", "in": "a2", "params": {"experts": 4, "top_k": 1, "hidden": 16, "out": 16}},
                          {"id": "a3", "op": "gelu", "in": "dec"},
                          {"id": "out", "op": "linear", "in": "a3", "params": {"out": features}}],
                "output": "out", "train": {"lr": 2e-3, "epochs": 80, "batch_size": 256, "patience": 6}}
    size = {"transfer": (32, 4, 64), "llm": (32, 4, 64), "memory": (48, 6, 96)}[kind]
    s = specs.moe_regressor(f"systune-{kind}", features, 1, dim=size[0], experts=size[1], top_k=2, hidden=size[2])
    s["train"].update({"epochs": 400 if kind != "memory" else 60, "patience": 8, "val_fraction": 0.2})
    return s


def train_model(kind: str, log: Log = lambda m: None) -> dict:
    """Build the dataset, train the kind's model on the GPU (a lab run); adopt() it once done (tick does)."""
    rows, ys = dataset(kind)
    if len(rows) < MIN_ROWS[kind]:
        raise LocalAIError(f"{kind}: {len(rows)} measurements so far, {MIN_ROWS[kind]} needed "
                           f"({'run the benchmark' if kind in ('transfer', 'llm') else 'telemetry is still collecting'})")
    names = list(rows[0])
    X = np.array([[r[k] for k in names] for r in rows], dtype=np.float32)
    d = home() / kind
    d.mkdir(exist_ok=True)
    path = d / f"data-{time.strftime('%Y%m%d-%H%M%S')}.npz"
    if kind == "stability":
        np.savez(path, X=X, names=np.array(names))
    else:
        np.savez(path, X=X, y=np.array(ys, dtype=np.float32), names=np.array(names))
    r = lab.start(design(kind, len(names)), {"path": str(path)}, label=f"system model: {kind}")
    st = state()
    st.setdefault(kind, {})["pending"] = r["id"]
    st[kind]["rows"] = len(rows)
    _save_state(st)
    log(f"{kind}: training on {len(rows)} measurements (run {r['id']})")
    return r


def adopt(kind: str, run_id: str) -> dict:
    s = lab.status(run_id)
    if s["state"] != "done":
        raise LocalAIError(f"run {run_id} is {s['state']}")
    exp = lab.export_of(run_id)
    st = state()
    cur = st.get(kind, {})
    st[kind] = {**cur, "run": run_id, "export": exp["dir"], "adopted": time.time(), "metrics": exp.get("metrics"), "pending": None}
    if kind == "stability":                       # the error level of normal data: risk is measured against it
        from bot.neurallab.infer import Model
        rows, _ = dataset(kind)
        m = Model.load(exp["dir"])
        err = m.anomaly(m.matrix(rows[-5000:]))
        st[kind]["baseline"] = {"p50": float(np.percentile(err, 50)), "p99": float(np.percentile(err, 99))}
    _save_state(st)
    _cache.pop(kind, None)
    return st[kind]


def tick(log: Log = lambda m: None) -> list[str]:
    """Adopt finished runs; retrain a kind when its data has grown by half (one GPU run at a time)."""
    done = []
    st = state()
    for kind in KINDS:
        k = st.get(kind) or {}
        if k.get("pending"):
            try:
                s = lab.status(k["pending"])
            except LocalAIError:
                s = {"state": "failed"}
            if s["state"] == "done":
                adopt(kind, k["pending"])
                done.append(f"{kind}: adopted run {k['pending']}")
            elif s["state"] not in lab.ACTIVE:
                st = state()
                st[kind]["pending"] = None
                st[kind]["last_error"] = s.get("error")
                _save_state(st)
    if any(r["state"] in lab.ACTIVE for r in lab.runs(10)):
        return done
    for kind in KINDS:
        k = state().get(kind) or {}
        if k.get("pending"):
            continue
        try:
            n = len(dataset(kind)[0])
        except (LocalAIError, OSError):
            continue
        if n >= MIN_ROWS[kind] and n >= 1.5 * (k.get("rows") or 0) and time.time() - (k.get("adopted") or 0) > 6 * 3600:
            try:
                train_model(kind, log)
                done.append(f"{kind}: retraining on {n} measurements")
            except LocalAIError as e:
                log(str(e))
            break
    return done


# ---- decisions ------------------------------------------------------------------------------------------------------------ #

def _measured() -> dict:
    """(drive, op, chunk_kb, threads) -> median MB/s of what was measured on this machine (cached for 10 minutes)."""
    hit = _cache.get("measured")
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    groups: dict = {}
    for r in telemetry.events("io_bench", 365 * 86400) + telemetry.events("io_copy", 365 * 86400):
        groups.setdefault((r["drive"], r["op"], int(r["chunk_kb"]), int(r["threads"])), []).append(float(r["mb_s"]))
    med = {k: float(np.median(v)) for k, v in groups.items()}
    _cache["measured"] = (time.time(), med)
    return med


def _predict_rate(m, info: dict, op: str, chunk_kb: int, threads: int, size_mb: float, drive: str = "") -> float:
    """This drive's own measurement for the setting when there is one; the model for settings never measured."""
    hit = _measured().get((drive, op, chunk_kb, threads)) if drive else None
    if hit is not None:
        return hit
    row = _transfer_row({**info, "op": op, "chunk_kb": chunk_kb, "threads": threads, "size_mb": size_mb, "busy_before": 0})
    return float(2 ** m.predict(row)[0, 0])


def advise_copy(src: str, dst: str, total_bytes: int = 1 << 30, files: int = 1) -> dict:
    """The block size and number of parallel files for copying src -> dst (local paths)."""
    m = model("transfer")
    if total_bytes < psutil.virtual_memory().available // 2:
        return {"chunk": 8 << 20, "parallel": 4, "source": "fits in free memory: the page cache takes it, settings hardly matter"}
    dv = drives(cached_only=True)
    s_drv, d_drv = drive_of(src), drive_of(dst)
    si, di = dv.get(s_drv, {}), dv.get(d_drv, {})
    if m is None or not si or not di:
        hdd = "HDD" in (si.get("media"), di.get("media"))
        return {"chunk": 8 << 20, "parallel": 1 if hdd else 4, "source": "default (no transfer model yet)"}
    size_mb = max(1.0, total_bytes / max(1, files) / 2**20)
    measured_threads = {k[3] for k in _measured()} or {1, 4}
    best = None
    for ck in CHUNKS_KB:
        for th in (1, 2, 4, 8):
            if th > max(1, files) or th > max(measured_threads):    # never past what was measured
                continue
            r = _predict_rate(m, si, "read", ck, th, size_mb, s_drv)
            w = _predict_rate(m, di, "write", ck, th, size_mb, d_drv)
            rate = 1 / (1 / r + 1 / w) if si.get("device") and si.get("device") == di.get("device") else min(r, w)
            if best is None or rate > best[0] * 1.03:           # a bigger setting must win by more than noise
                best = (rate, ck, th)
    return {"chunk": best[1] << 10, "parallel": best[2], "predicted_mb_s": round(float(best[0]), 1),
            "source": "this machine's drive measurements and the transfer model",
            "src": s_drv, "dst": d_drv}


def advise_llm(name: str) -> dict:
    """Batch, micro-batch and flash attention for running a model on this GPU (best generation speed, then prompt)."""
    m = model("llm")
    if m is None:
        return {}
    from bot.localai import gguf, models
    rec = models.resolve(name)
    s = gguf.summary(gguf.read(rec["weights"], tensors=True))
    if s.get("embedding_model"):
        return {}
    size = os.path.getsize(rec["weights"])
    params = float(s.get("parameters") or 0) or size * 8 / 4.5
    base = {"params_b": params / 1e9, "size_gb": size / 2**30, "bits": size * 8 / params}
    best = None
    for b in (512, 1024, 2048):
        for ub in (128, 256, 512):
            if ub > b:
                continue
            for fa in (1.0, 0.0):
                g = 2 ** m.predict(_llm_row({**base, "n_batch": b, "n_ubatch": ub, "flash_attn": fa, "test": "gen", "n_tokens": 128}))[0, 0]
                p = 2 ** m.predict(_llm_row({**base, "n_batch": b, "n_ubatch": ub, "flash_attn": fa, "test": "prompt", "n_tokens": 512}))[0, 0]
                score = math.log(g) * 0.7 + math.log(p) * 0.3
                if best is None or score > best[0]:
                    best = (score, b, ub, fa, g, p)
    return {"num_batch": best[1], "num_ubatch": best[2], "flash_attn": bool(best[3]),
            "predicted_gen_tok_s": round(float(best[4]), 1), "predicted_prompt_tok_s": round(float(best[5]), 1)}


def forecast_memory() -> Optional[dict]:
    m = model("memory")
    hist = telemetry.history(600)
    if m is None or len(hist) < 10:
        return None
    ts = np.array([h["t"] for h in hist])
    ram = np.array([h["ram_used"] for h in hist], dtype=float)
    f = _mem_features(hist, len(hist) - 1, ts, ram)
    return {"now": float(ram[-1]), "in_5_min": round(float(m.predict(f)[0, 0]), 1)}


def cpu_policy() -> dict:
    """Which logical processors ABP's heavy work may use, and how many threads: the cores with machine-check errors
    are left out (both SMT threads of the physical core), and the thread cap drops when risk is high."""
    now = time.time()
    hit = _cache.get("policy")
    if hit and now - hit[0] < 300:
        return hit[1]
    topo = telemetry.topology()
    err = telemetry.hw_errors(days=180)
    bad_cores = set()
    reasons = []
    for b in err["by_apic"]:
        if b["fatal"] and now - b["last"] < 180 * 86400:
            core = next((r["core"] for r in topo if r["apic"] == b["apic"]), None)
            if core is not None:
                bad_cores.add(core)
                reasons.append(f"APIC {b['apic']} (CPU {b['logical']}): {b['errors']} machine-check error(s), "
                               f"last {time.strftime('%Y-%m-%d %H:%M', time.localtime(b['last']))}")
    avoid = sorted(r["logical"] for r in topo if r["core"] in bad_cores)
    allowed = [r["logical"] for r in topo if r["logical"] not in avoid]
    recent_mce = any(m["fatal"] and now - m["time"] < 7 * 86400 for m in err["machine_checks"])
    recent_loss = any(now - t < 86400 for t in err["power_losses"])
    level, threads = "normal", CPU_THREADS
    anomaly = None
    m = model("stability")
    base = (state().get("stability") or {}).get("baseline")
    hist = telemetry.history(120)
    if m is not None and base and hist:
        e = float(m.anomaly(m.matrix([_stab_row(h) for h in hist[-6:]])).mean())
        anomaly = round(e / max(1e-6, base["p99"]), 2)
    if recent_mce or recent_loss:
        level, threads = "high", max(1, CPU_THREADS // 2)
        reasons.append("a machine-check error in the last 7 days" if recent_mce else "an unexpected power loss in the last 24 hours")
    elif anomaly is not None and anomaly > 1.5:
        level, threads = "elevated", max(2, CPU_THREADS - 1)
        reasons.append(f"load pattern unusual for this machine ({anomaly}x the normal reconstruction error)")
    elif bad_cores:
        level = "elevated"
    pol = {"level": level, "threads": threads, "avoid": avoid, "allowed": allowed, "anomaly": anomaly, "reasons": reasons,
           "power_losses_30d": sum(1 for t in err["power_losses"] if now - t < 30 * 86400)}
    _cache["policy"] = (now, pol)
    return pol


def guard_process(pid: int) -> Optional[list[int]]:
    """Keep one of ABP's own processes off the processors the policy avoids (no effect when none are)."""
    try:
        pol = cpu_policy()
        if not pol["avoid"]:
            return None
        p = psutil.Process(pid)
        p.cpu_affinity(pol["allowed"])
        return pol["allowed"]
    except (psutil.Error, OSError, LocalAIError, ValueError):
        return None


def status() -> dict:
    st = state()
    out = {}
    for kind in KINDS:
        k = st.get(kind) or {}
        try:
            n = len(dataset(kind)[0])
        except (LocalAIError, OSError, ValueError):
            n = 0
        out[kind] = {"measurements": n, "needed": MIN_ROWS[kind], "adopted": k.get("adopted"), "run": k.get("run"),
                     "pending": k.get("pending"), "metrics": k.get("metrics"), "last_error": k.get("last_error")}
    return {"models": out, "cpu_policy": cpu_policy(), "telemetry": telemetry.stats()}

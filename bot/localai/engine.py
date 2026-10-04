"""The inference engine: llama.cpp, one `llama-server` per loaded model.

Install   install(backend) downloads ggml-org/llama.cpp's official release build for this OS and GPU into
          <home>/engines/<build>-<backend>/ (vulkan: any modern GPU; hip: AMD ROCm; cuda: NVIDIA; cpu; metal on Macs).
          `auto` picks HIP or Vulkan on AMD, CUDA on NVIDIA, Metal on Apple silicon, else CPU.
Load      load(name, options) starts llama-server for the model on a free local port: every layer on the GPU
          (num_gpu), the context length (num_ctx), the projector for images, LoRA adapters, --jinja chat templates
          (tool calls, reasoning), embeddings mode for embedding models; CPU threads capped. It waits until the
          model answers /health.
Unload    after keep_alive of no use (default 5 minutes; 0 = at once; negative = never), when the model limit or
          the GPU memory budget needs room (least recently used first), or on request.
"""
from __future__ import annotations

import json
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional

import httpx

from bot.localai import gguf, models
from bot.localai.paths import LocalAIError, cpu_threads, home, sub
from bot.sandbox_ns import spawn

RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
EXE = ".exe" if sys.platform == "win32" else ""


# ---- settings, hardware --------------------------------------------------------------------------------------------- #

def settings() -> dict:
    p = home() / "state.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    return {"backend": "auto", "keep_alive_s": 300, "max_loaded": 3, "gpu_memory_gb": 0, "default_ctx": 8192,
            "parallel": 2, "flash_attention": True, "auto_tune": True, **st}


def set_settings(changes: dict) -> dict:
    allowed = {"backend", "keep_alive_s", "max_loaded", "gpu_memory_gb", "default_ctx", "parallel", "flash_attention",
               "port", "bind", "autostart", "auto_tune", "scan_folders", "lab_telemetry", "lab_telemetry_interval_s",
               "lab_auto_retrain"}
    bad = set(changes) - allowed
    if bad:
        raise LocalAIError(f"unknown setting(s): {', '.join(sorted(bad))}")
    st = {**settings(), **changes}
    (home() / "state.json").write_text(json.dumps(st, indent=1), encoding="utf-8")
    return st


def gpus() -> list[dict]:
    out = []
    if sys.platform == "win32":
        try:
            import winreg
            base = r"SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
                for i in range(64):
                    try:
                        sk = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(k, sk) as d:
                            name = winreg.QueryValueEx(d, "DriverDesc")[0]
                            try:
                                mem = int(winreg.QueryValueEx(d, "HardwareInformation.qwMemorySize")[0])
                            except OSError:
                                mem = 0
                            if mem:
                                out.append({"name": name, "memory": mem, "vendor": _vendor(name)})
                    except OSError:
                        continue
        except ImportError:
            pass
    else:
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                n, m = [x.strip() for x in line.split(",")]
                out.append({"name": n, "memory": int(float(m)) << 20, "vendor": "nvidia"})
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not out:
            for p in Path("/sys/class/drm").glob("card*/device/mem_info_vram_total"):
                try:
                    out.append({"name": "AMD GPU", "memory": int(p.read_text()), "vendor": "amd"})
                except (OSError, ValueError):
                    pass
    return out


def _vendor(name: str) -> str:
    n = name.lower()
    return "nvidia" if "nvidia" in n or "geforce" in n or "rtx" in n else "amd" if "amd" in n or "radeon" in n else \
        "intel" if "intel" in n or "arc" in n else "other"


def gpu_budget() -> int:
    st = settings()
    if st["gpu_memory_gb"]:
        return int(st["gpu_memory_gb"] * (1 << 30))
    g = [x for x in gpus() if x["vendor"] in ("amd", "nvidia", "intel")]
    return int(max((x["memory"] for x in g), default=0) * 0.92)


# ---- install ------------------------------------------------------------------------------------------------------- #

def installed() -> list[dict]:
    out = []
    for d in sorted(sub("engines").iterdir()) if sub("engines").exists() else []:
        exe = next(iter(d.rglob(f"llama-server{EXE}")), None)
        if exe:
            meta = {}
            try:
                meta = json.loads((d / "abp-engine.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            out.append({"dir": str(d), "server": str(exe), "backend": meta.get("backend", d.name.rsplit("-", 1)[-1]),
                        "build": meta.get("build", d.name.split("-")[0]), "installed": meta.get("installed"),
                        "build_number": int(re.sub(r"\D", "", meta.get("build", "0")) or 0)})
    return out


def _want_backend() -> list[str]:
    b = settings()["backend"]
    if b != "auto":
        return [b]
    if sys.platform == "darwin":
        return ["metal", "cpu"]
    vendors = {g["vendor"] for g in gpus()}
    if "nvidia" in vendors:
        return ["cuda", "vulkan", "cpu"]
    if "amd" in vendors:
        return ["hip", "vulkan", "cpu"]
    if vendors - {"other"}:
        return ["vulkan", "cpu"]
    return ["cpu"]


def _asset_matches(name: str, backend: str) -> bool:
    """llama.cpp's release asset names: llama-<build>-bin-<os>-<variant>-<arch>.(zip|tar.gz), e.g.
    llama-b11312-bin-win-vulkan-x64.zip, -win-rocm-10.0-x64.zip, -win-cuda-12.4-x64.zip, -ubuntu-vulkan-x64.tar.gz."""
    n = name.lower()
    if not n.startswith("llama-") or not (n.endswith(".zip") or n.endswith(".tar.gz")) or "-bin-" not in n:
        return False
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    if not n.split(".")[0].endswith(arch) and f"-{arch}." not in n:
        return False
    if sys.platform == "win32":
        osok = "-win-" in n
    elif sys.platform == "darwin":
        osok = "-macos-" in n
    else:
        osok = "-ubuntu-" in n or "-linux-" in n
    if not osok or "snapdragon" in n or "adreno" in n:
        return False
    if backend == "vulkan":
        return "-vulkan-" in n
    if backend == "hip":
        return "-rocm-" in n or "-hip-" in n
    if backend == "cuda":
        return "-cuda-" in n
    if backend == "metal":
        return "-macos-" in n
    return "-cpu-" in n or bool(re.search(r"-bin-(ubuntu|linux)-(x64|arm64)\.tar\.gz$", n))


def _extract(archive: Path, dest: Path) -> None:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    else:
        import tarfile
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data")


def install(backend: str = "", log=lambda m: None) -> dict:
    """Download and unpack llama.cpp's newest official release build for `backend` (or the best one for this GPU),
    checked against the SHA-256 digest GitHub publishes for the asset."""
    with httpx.Client(timeout=60, follow_redirects=True, headers={"Accept": "application/vnd.github+json", "User-Agent": "ABP-LocalAI"}) as c:
        rels = c.get(f"{RELEASES}?per_page=8").json()
        if not isinstance(rels, list):
            raise LocalAIError(f"GitHub did not list llama.cpp's releases: {rels}")
        tries = [backend] if backend else _want_backend()
        for b in tries:
            found = None
            for rel in rels:                  # the newest release that already has this build (uploads take a while)
                if rel.get("draft"):          # (llama.cpp publishes every build as a pre-release)
                    continue
                asset = next((a for a in rel.get("assets", []) if _asset_matches(a["name"], b)), None)
                if asset:
                    found = (rel, asset)
                    break
            if not found:
                log(f"no recent llama.cpp release has a {b} build for this system")
                continue
            rel, asset = found
            tag = rel["tag_name"]
            dest = sub("engines") / f"{tag}-{b}"
            if (dest / "abp-engine.json").exists():
                log(f"llama.cpp {tag} ({b}) is already installed")
                return next(e for e in installed() if e["dir"] == str(dest))
            zpath = sub("engines") / asset["name"]
            log(f"downloading {asset['name']} ({asset['size'] >> 20} MiB) from github.com/ggml-org/llama.cpp ({tag})")
            with c.stream("GET", asset["browser_download_url"]) as r:
                r.raise_for_status()
                with open(zpath, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
            if (asset.get("digest") or "").startswith("sha256:"):
                if models.sha256_file(zpath) != asset["digest"]:
                    zpath.unlink()
                    raise LocalAIError(f"{asset['name']} does not match the digest GitHub lists for it")
                log("sha256 verified against the release's published digest")
            dest.mkdir(parents=True, exist_ok=True)
            _extract(zpath, dest)
            zpath.unlink()
            if b == "cuda" and sys.platform == "win32":     # the CUDA runtime comes as a separate archive
                ver = asset["name"].split("-cuda-")[1].split("-")[0]
                rt = next((a for a in rel["assets"] if a["name"].startswith("cudart-") and f"cuda-{ver}" in a["name"] and "-win-" in a["name"]), None)
                if rt:
                    rz = sub("engines") / rt["name"]
                    with c.stream("GET", rt["browser_download_url"]) as r, open(rz, "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
                    _extract(rz, dest)
                    rz.unlink()
            if not EXE:
                for exe in dest.rglob("llama-*"):
                    exe.chmod(0o755)
            (dest / "abp-engine.json").write_text(json.dumps({"build": tag, "backend": b, "asset": asset["name"],
                                                              "digest": asset.get("digest"), "installed": int(time.time())}), encoding="utf-8")
            log(f"installed llama.cpp {tag} ({b}) in {dest}")
            return next(e for e in installed() if e["dir"] == str(dest))
    raise LocalAIError(f"no llama.cpp release build fits this system ({', '.join(tries)})")


def engine() -> dict:
    eng = installed()
    if not eng:
        raise LocalAIError("the llama.cpp engine is not installed: install it on the Local AI page or `abp ai engine install`")
    for b in _want_backend():
        hits = [e for e in eng if e["backend"] == b]
        if hits:
            return sorted(hits, key=lambda e: e["build_number"])[-1]
    return eng[-1]


def tool(name: str) -> str:
    """Another llama.cpp program from the engine build (llama-quantize, llama-cli, ...)."""
    d = Path(engine()["dir"])
    exe = next(iter(d.rglob(f"{name}{EXE}")), None)
    if not exe:
        raise LocalAIError(f"{name} is not part of the installed llama.cpp build")
    return str(exe)


# ---- runners ------------------------------------------------------------------------------------------------------- #

class Runner:
    def __init__(self, key: str, rec: dict, opts: dict, port: int, proc: subprocess.Popen, info: dict, est: int):
        self.key, self.rec, self.opts, self.port, self.proc, self.info, self.est = key, rec, opts, port, proc, info, est
        self.url = f"http://127.0.0.1:{port}"
        self.last_used = time.time()
        self.busy = 0
        self.expires = 0.0
        self.started = time.time()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


_runners: dict[str, Runner] = {}
_lock = threading.RLock()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _key(name: str, opts: dict) -> str:
    return f"{models.canonical(name)}|ctx={opts.get('num_ctx')}|emb={bool(opts.get('embedding'))}"


def estimate(rec: dict, ctx: int, parallel: int) -> tuple[int, dict]:
    info = gguf.summary(gguf.read(rec["weights"]))
    size = Path(rec["weights"]).stat().st_size + (Path(rec["projector"]).stat().st_size if rec.get("projector") else 0)
    size += sum(Path(a).stat().st_size for a in rec.get("adapters") or [])
    kv = gguf.kv_cache_bytes(info, ctx * max(1, parallel) if not info["embedding_model"] else 0)
    return size + kv + (300 << 20), info


def _log_path(key: str) -> Path:
    return sub("logs") / ("runner-" + "".join(ch if ch.isalnum() else "_" for ch in key)[:80] + ".log")


def load(name: str, options: Optional[dict] = None, keep_alive: Optional[float] = None, embedding: bool = False) -> Runner:
    rec = models.resolve(name)
    st = settings()
    opts = dict(rec.get("params") or {})
    opts.update(options or {})
    est_info = gguf.summary(gguf.read(rec["weights"], tensors=False))
    embedding = embedding or est_info["embedding_model"]
    trained = est_info.get("context_length") or st["default_ctx"]
    ctx = int(opts.get("num_ctx") or min(st["default_ctx"], trained))
    opts["num_ctx"], opts["embedding"] = ctx, embedding
    key = _key(name, opts)
    with _lock:
        r = _runners.get(key)
        if r and r.alive():
            r.last_used = time.time()
            r.expires = _expiry(keep_alive)
            return r
        if r:
            _runners.pop(key, None)
        parallel = 1 if embedding else int(st["parallel"])
        est, info = estimate(rec, ctx, parallel)
        budget = gpu_budget()
        while _runners and (len(_runners) >= int(st["max_loaded"]) or (budget and sum(x.est for x in _runners.values()) + est > budget)):
            victim = min((x for x in _runners.values() if not x.busy), key=lambda x: x.last_used, default=None)
            if not victim:
                break
            victim.stop()
            _runners.pop(victim.key, None)
        eng = engine()
        port = _free_port()
        threads = cpu_threads()
        argv = [eng["server"], "-m", rec["weights"], "--host", "127.0.0.1", "--port", str(port), "-c", str(ctx),
                "--threads", str(threads), "--threads-batch", str(threads), "--alias", models.canonical(name),
                "--no-webui", "-np", str(parallel)]
        if eng["backend"] != "cpu":
            argv += ["-ngl", str(int(opts.get("num_gpu", 999)))]
        if embedding:
            argv += ["--embeddings"]
            if info["architecture"] in ("nomic-bert", "bert", "jina-bert-v2"):
                argv += ["--pooling", "mean"]
            argv += ["-ub", str(max(512, ctx)), "-b", str(max(512, ctx))]
        else:
            argv += ["--jinja"]
            tuned = _tuned(name, opts) if st.get("auto_tune", True) and eng["backend"] != "cpu" else {}
            fa = tuned.get("flash_attn", st["flash_attention"])
            if fa and eng["backend"] != "cpu":
                argv += ["-fa", "on"]
            if opts.get("num_batch") or tuned.get("num_batch"):
                argv += ["-b", str(int(opts.get("num_batch") or tuned["num_batch"]))]
            if tuned.get("num_ubatch"):
                argv += ["-ub", str(int(tuned["num_ubatch"]))]
        if rec.get("projector"):
            argv += ["--mmproj", rec["projector"]]
        for a in rec.get("adapters") or []:
            argv += ["--lora", a]
        if opts.get("seed") is not None:
            argv += ["--seed", str(int(opts["seed"]))]
        log = open(_log_path(key), "ab")
        log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(argv)}\n".encode())
        log.flush()
        # preset "engine": one llama-server per model, windowless, below-normal priority, off the
        # processors with a machine-check history, and capped at a share of the machine's CPU
        # (it is the one process of ours allowed to be big; its own --threads stays the real limit).
        flags = {"creationflags": _NO_WINDOW} if sys.platform == "win32" else {}     # POSIX Popen has no such flag
        proc = spawn.spawn(argv, preset="engine", name=f"llama-server:{models.canonical(name)}", owner="localai.engine",
                           cwd=Path(eng["server"]).parent, stdin=subprocess.DEVNULL, stdout=log,
                           stderr=subprocess.STDOUT, **flags)
        log.close()
        r = Runner(key, rec, opts, port, proc, info, est)
        r.expires = _expiry(keep_alive)
        _runners[key] = r
    deadline = time.time() + 600
    while time.time() < deadline:
        if not r.alive():
            with _lock:
                _runners.pop(key, None)
            tail = _log_path(key).read_text(encoding="utf-8", errors="replace")[-2000:]
            raise LocalAIError(f"the engine could not load {models.canonical(name)}:\n{tail}")
        try:
            h = httpx.get(f"{r.url}/health", timeout=2)
            if h.status_code == 200:
                r.load_seconds = time.time() - r.started
                return r
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    r.stop()
    raise LocalAIError(f"{models.canonical(name)} did not finish loading in 10 minutes")


def _tuned(name: str, opts: dict) -> dict:
    """Batch, micro-batch and flash attention from the LLM runtime model (bot/neurallab/systune.py), measured on this
    GPU with llama-bench; {} until that model exists. A model's own options (Modelfile PARAMETER num_batch) win."""
    try:
        from bot.neurallab import systune
        return systune.advise_llm(name)
    except Exception:                                     # noqa: BLE001 - no advice: llama.cpp's defaults
        return {}


def _expiry(keep_alive: Optional[float]) -> float:
    ka = settings()["keep_alive_s"] if keep_alive is None else keep_alive
    if ka < 0:
        return float("inf")
    return time.time() + ka


def parse_keep_alive(v) -> Optional[float]:
    """Ollama's keep_alive: seconds (number) or a duration string ('5m', '1h', '30s', '-1')."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    mult = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    for suf in ("ms", "s", "m", "h"):
        if s.endswith(suf):
            try:
                return float(s[: -len(suf)]) * mult[suf]
            except ValueError:
                break
    try:
        return float(s)
    except ValueError:
        raise LocalAIError(f"keep_alive {v!r} is not a duration") from None


def unload(name: str = "") -> int:
    n = 0
    with _lock:
        for k, r in list(_runners.items()):
            if not name or r.rec["name"] == models.canonical(name):
                r.stop()
                _runners.pop(k, None)
                n += 1
    return n


MEMORY_HIGH = 90.0          # % of RAM: when the forecast says use will pass this, idle models go early


def reap() -> None:
    now = time.time()
    with _lock:
        for k, r in list(_runners.items()):
            if not r.alive() or (not r.busy and now > r.expires):
                r.stop()
                _runners.pop(k, None)
        if not _runners:
            return
    fc = _memory_forecast()
    if fc and fc["in_5_min"] >= MEMORY_HIGH:
        with _lock:                                       # the least recently used idle model first, one per pass
            victim = min((x for x in _runners.values() if not x.busy), key=lambda x: x.last_used, default=None)
            if victim:
                victim.stop()
                _runners.pop(victim.key, None)


def _memory_forecast() -> Optional[dict]:
    """RAM use five minutes ahead (bot/neurallab/systune.py), when that model exists."""
    try:
        from bot.neurallab import systune
        return systune.forecast_memory()
    except Exception:                                     # noqa: BLE001
        return None


def running() -> list[dict]:
    with _lock:
        rs = list(_runners.values())
    return [{"name": r.rec["name"], "model": r.rec["name"], "size": r.est, "size_vram": r.est if r.opts.get("num_gpu", 999) else 0,
             "digest": r.rec.get("digest", ""), "context_length": r.opts["num_ctx"], "embedding": r.opts["embedding"],
             "expires_at": models._iso(4102444800 if r.expires == float("inf") else r.expires),
             "details": {"family": r.info.get("architecture"), "parameter_size": r.info.get("parameter_size"),
                         "quantization_level": r.info.get("quantization")}, "port": r.port} for r in rs]

"""Find the models other software already keeps on this machine, and let ABP use them.

    Ollama             its store (OLLAMA_MODELS, ~/.ollama/models): the same layout as ABP's, so it is added as an
                       extra store and read in place — every Ollama model is runnable from ABP without a copy
    LM Studio          ~/.lmstudio/models, ~/.cache/lm-studio/models (publisher/repo/file.gguf)
    Hugging Face       the hub cache (HF_HUB_CACHE, HF_HOME/hub, ~/.cache/huggingface/hub): GGUF files are runnable;
                       safetensors checkpoints are base models for fine-tuning (bot/localai/train.py), or convertible
    GPT4All, Jan, Kestrion   their model folders (Kestrion: ~/.kestrion/models; Kestrion reads ABP's store back)
    text-generation-webui, KoboldCpp, llamafile, any folder
                       folders a person adds (settings "scan_folders")

scan() lists what is there; adopt() makes one usable:
    "store"      an Ollama-format folder becomes an extra store (read in place)
    "reference"  a GGUF is used where it is (no copy, nothing to clean up; it goes away if the other program deletes it)
    "import"     a GGUF is brought into ABP's own store (hard link on the same drive, else a copy)
A new user's existing collection is therefore a slot-in replacement: point ABP at it, nothing is downloaded again.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

from bot.localai import engine, gguf, models
from bot.localai.paths import LocalAIError

_HOME = Path.home()
_MIN_GGUF = 1 << 20             # skip vocab-only test files and partial downloads


def _env_path(name: str) -> Optional[Path]:
    v = os.environ.get(name, "").strip()
    return Path(v) if v else None


def _ollama_dirs() -> list[Path]:
    out = [_env_path("OLLAMA_MODELS"), _HOME / ".ollama" / "models"]
    if sys.platform == "win32":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                sub = "Environment" if hive == winreg.HKEY_CURRENT_USER else r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
                with winreg.OpenKey(hive, sub) as k:
                    out.append(Path(winreg.QueryValueEx(k, "OLLAMA_MODELS")[0]))
            except OSError:
                pass
        # Ollama installed somewhere else with its models beside it (a moved profile, another drive)
        for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            p = Path(f"{drive}:/Users/{_HOME.name}/.ollama/models")
            if drive != _HOME.drive[:1] and p.is_dir():
                out.append(p)
    else:
        out += [Path("/usr/share/ollama/.ollama/models"), Path("/var/lib/ollama/models")]
    return out


def _hf_dirs() -> list[Path]:
    hf_home = _env_path("HF_HOME")
    return [p for p in (_env_path("HF_HUB_CACHE"), _env_path("HUGGINGFACE_HUB_CACHE"), hf_home / "hub" if hf_home else None,
                        _HOME / ".cache" / "huggingface" / "hub") if p]


def known_locations() -> list[dict]:
    appdata = Path(os.environ.get("APPDATA", _HOME / "AppData" / "Roaming"))
    local = Path(os.environ.get("LOCALAPPDATA", _HOME / "AppData" / "Local"))
    locs = [{"app": "Ollama", "kind": "ollama", "path": p} for p in _ollama_dirs()]
    locs += [{"app": "LM Studio", "kind": "folder", "path": p} for p in
             (_HOME / ".lmstudio" / "models", _HOME / ".cache" / "lm-studio" / "models")]
    locs += [{"app": "Hugging Face", "kind": "hf", "path": p} for p in _hf_dirs()]
    locs += [{"app": "GPT4All", "kind": "folder", "path": p} for p in
             (local / "nomic.ai" / "GPT4All", _HOME / ".local" / "share" / "nomic.ai" / "GPT4All",
              _HOME / "Library" / "Application Support" / "nomic.ai" / "GPT4All")]
    locs += [{"app": "Jan", "kind": "folder", "path": p} for p in
             (_HOME / "jan" / "models", appdata / "Jan" / "data" / "models", _HOME / ".local" / "share" / "Jan" / "data" / "models")]
    locs += [{"app": "Kestrion", "kind": "folder", "path": _HOME / ".kestrion" / "models"}]
    locs += [{"app": "Folder", "kind": "folder", "path": Path(p)} for p in engine.settings().get("scan_folders", [])]
    seen, out = set(), []
    for l in locs:
        if l["path"] is None:
            continue
        key = os.path.normcase(str(l["path"].resolve() if l["path"].exists() else l["path"]))
        if key not in seen:
            seen.add(key)
            out.append({**l, "path": l["path"], "exists": l["path"].is_dir()})
    return out


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.")
    return (s or "model")[:120]


def suggest_name(app: str, path: Path) -> str:
    """A model name for a GGUF found on disk: <app>/<repo>:<quant>, e.g. lmstudio/qwen2.5-7b-instruct:q4_k_m."""
    stem = path.stem
    m = re.search(r"[.-]((?:I?Q\d[_A-Z0-9]*)|F16|BF16|F32)$", stem, re.I)
    tag = m.group(1).lower() if m else "latest"
    repo = stem[:m.start()] if m else stem
    shard = re.search(r"-(\d{5})-of-\d{5}$", repo)
    if shard:
        repo = repo[:shard.start()]
    ns = {"LM Studio": "lmstudio", "Hugging Face": "hf-cache", "GPT4All": "gpt4all", "Jan": "jan", "Kestrion": "kestrion"}.get(app, "local")
    return f"{ns}/{_slug(repo).lower()}:{_slug(tag)}"


def _gguf_item(app: str, p: Path, root: Path) -> Optional[dict]:
    try:
        st = p.stat()
        if st.st_size < _MIN_GGUF or "mmproj" in p.name.lower():
            return None
        if re.search(r"-0000[2-9]-of-|-000[1-9]\d-of-", p.name):    # later shards of a split model: the first one loads all
            return None
        s = gguf.summary(gguf.read(p, tensors=False))
    except (OSError, LocalAIError, ValueError):
        return None
    proj = next((q for q in p.parent.glob("*mmproj*.gguf")), None)
    return {"app": app, "kind": "gguf", "path": str(p), "size": st.st_size, "name": suggest_name(app, p),
            "architecture": s.get("architecture", ""), "parameter_size": s.get("parameter_size", ""),
            "quantization": s.get("quantization", ""), "projector": str(proj) if proj else "",
            "actions": ["reference", "import"]}


def _scan_folder(app: str, root: Path, limit: int) -> list[dict]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if f.lower().endswith(".gguf"):
                item = _gguf_item(app, Path(dirpath) / f, root)
                if item:
                    out.append(item)
                    if len(out) >= limit:
                        return out
    return out


def _scan_hf(root: Path, limit: int) -> list[dict]:
    out = []
    for repo in sorted(root.glob("models--*")):
        repo_id = repo.name[len("models--"):].replace("--", "/")
        snaps = sorted((repo / "snapshots").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not snaps:
            continue
        snap = snaps[0]
        ggufs = list(snap.rglob("*.gguf"))
        for g in ggufs:
            item = _gguf_item("Hugging Face", g, root)
            if item:
                item["repo"] = repo_id
                out.append(item)
        st = list(snap.glob("*.safetensors"))
        if st and (snap / "config.json").exists():
            size = sum(p.resolve().stat().st_size for p in st if p.exists())
            out.append({"app": "Hugging Face", "kind": "safetensors", "path": str(snap), "repo": repo_id, "size": size,
                        "name": f"hf-cache/{_slug(repo_id.split('/')[-1]).lower()}:f16",
                        "quantized_4bit": "bnb-4bit" in repo_id.lower(),
                        "tokenizer": (snap / "tokenizer.json").exists() or (snap / "tokenizer.model").exists()
                        or (snap / "vocab.json").exists(),
                        "actions": ["train-base", "convert"]})
        if len(out) >= limit:
            break
    return out


def scan(extra: Optional[list[str]] = None, limit: int = 500) -> dict:
    """Every model found on this machine, with which are already usable from ABP."""
    stores = {os.path.normcase(s["path"]) for s in models.extra_stores()}
    refs = {os.path.normcase(r["weights"]) for r in models._refs().values()}
    locations, found = [], []
    locs = known_locations() + [{"app": "Folder", "kind": "folder", "path": Path(p), "exists": Path(p).is_dir()} for p in extra or []]
    own = os.path.normcase(str(models.store_root()))
    for loc in locs:
        p: Path = loc["path"]
        entry = {"app": loc["app"], "path": str(p), "exists": loc["exists"], "models": 0}
        locations.append(entry)
        if not loc["exists"] or os.path.normcase(str(p)) == own:
            continue
        if loc["kind"] == "ollama" or (p / "manifests").is_dir():
            names = models._walk_manifests(p)
            entry["models"] = len(names)
            if names:
                found.append({"app": loc["app"], "kind": "ollama-store", "path": str(p), "models": [n for n, _ in names],
                              "adopted": os.path.normcase(str(p)) in stores, "actions": ["store"]})
            continue
        items = _scan_hf(p, limit) if loc["kind"] == "hf" else _scan_folder(loc["app"], p, limit)
        for it in items:
            it["adopted"] = os.path.normcase(it["path"]) in refs
        entry["models"] = len(items)
        found += items
    return {"locations": locations, "found": found}


def adopt(item: dict, action: str = "", name: str = "") -> dict:
    """Make one found model usable: an Ollama store read in place, a GGUF referenced or imported."""
    kind = item.get("kind")
    action = action or {"ollama-store": "store", "gguf": "reference"}.get(kind, "")
    if kind == "ollama-store" and action == "store":
        cur = models.extra_stores()
        if not any(os.path.normcase(s["path"]) == os.path.normcase(item["path"]) for s in cur):
            cur.append({"name": item.get("app", "Ollama"), "path": item["path"]})
            models.set_extra_stores(cur)
        return {"action": "store", "path": item["path"], "models": item.get("models", [])}
    if kind == "gguf":
        nm = name or item.get("name") or suggest_name(item.get("app", "Folder"), Path(item["path"]))
        if action == "reference":
            return {"action": "reference", **models.add_reference(nm, item["path"], origin=item.get("app", ""),
                                                                  projector=item.get("projector") or "")}
        if action == "import":
            return {"action": "import", **models.import_file(nm, item["path"], projector=item.get("projector") or "")}
    if kind == "safetensors" and action == "convert":
        from bot.localai import train
        return {"action": "convert", **train.convert_to_gguf(item["path"], name or item["name"])}
    raise LocalAIError(f"can't {action or 'adopt'} a {kind}")


def adopt_all(action_for_gguf: str = "reference") -> list[dict]:
    """A new user's first run: every Ollama store read in place, every GGUF elsewhere referenced."""
    done = []
    for it in scan()["found"]:
        if it.get("adopted") or it["kind"] not in ("ollama-store", "gguf"):
            continue
        try:
            done.append(adopt(it, "store" if it["kind"] == "ollama-store" else action_for_gguf))
        except LocalAIError as e:
            done.append({"error": str(e), "path": it["path"]})
    return done

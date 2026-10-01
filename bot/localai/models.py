"""ABP's model store, laid out exactly like Ollama's, so each can read the other's:

    <home>/models/manifests/<host>/<namespace>/<model>/<tag>     an OCI manifest (JSON): a config blob and layers
    <home>/models/blobs/sha256-<hex>                            the layers: weights (GGUF), projector, adapters,
                                                               template, system prompt, parameters, messages, license
    <home>/models/abp-references.json                          models used in place from other folders (not copied)

Names follow Ollama: "qwen2.5:0.5b" is registry.ollama.ai/library/qwen2.5:0.5b, "me/model:tag" a namespace,
"hf.co/<org>/<repo>:<quant>" Hugging Face; the tag defaults to "latest".

A model comes in three ways:
    pulled      downloaded into the store (bot/localai/pull.py)
    imported    a GGUF (or another program's model) copied — or hard-linked when on the same drive — into the store
    referenced  used where it is (another program's folder, a big drive), recorded in abp-references.json
Other stores (an Ollama folder, LM Studio, the Hugging Face cache...) can be read as extra sources too
(bot/localai/discover.py); their models show up under their own names and can be referenced or imported.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from bot.localai import gguf
from bot.localai.paths import LocalAIError, home, sub

MT = {
    "model": "application/vnd.ollama.image.model", "projector": "application/vnd.ollama.image.projector",
    "adapter": "application/vnd.ollama.image.adapter", "template": "application/vnd.ollama.image.template",
    "system": "application/vnd.ollama.image.system", "params": "application/vnd.ollama.image.params",
    "messages": "application/vnd.ollama.image.messages", "license": "application/vnd.ollama.image.license",
}
CONFIG_MT = "application/vnd.docker.container.image.v1+json"
MANIFEST_MT = "application/vnd.docker.distribution.manifest.v2+json"
DEFAULT_HOST, DEFAULT_NS = "registry.ollama.ai", "library"
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def store_root(base: Optional[Path] = None) -> Path:
    return base or sub("models")


# ---- names -------------------------------------------------------------------------------------------------------- #

def parse_name(name: str) -> tuple[str, str, str, str]:
    """'qwen2.5:0.5b' -> (registry.ollama.ai, library, qwen2.5, 0.5b); 'hf.co/org/repo:Q4_K_M' -> (hf.co, org, repo, Q4_K_M)."""
    n = name.strip()
    for pre in ("https://", "http://"):
        n = n.removeprefix(pre)
    n = n.replace("huggingface.co/", "hf.co/")
    tag = "latest"
    last = n.rsplit("/", 1)[-1]
    if ":" in last:
        n, tag = n.rsplit(":", 1)
    parts = n.split("/")
    if len(parts) == 1:
        host, ns, model = DEFAULT_HOST, DEFAULT_NS, parts[0]
    elif len(parts) == 2:
        host, ns, model = DEFAULT_HOST, parts[0], parts[1]
    else:
        host, ns, model = parts[0], "/".join(parts[1:-1]), parts[-1]
    for p in (model, tag):
        if not _PART.match(p):
            raise LocalAIError(f"{name!r} is not a model name (like qwen2.5:0.5b or hf.co/org/repo:Q4_K_M)")
    return host, ns, model, tag


def short_name(host: str, ns: str, model: str, tag: str) -> str:
    if host == DEFAULT_HOST and ns == DEFAULT_NS:
        base = model
    elif host == DEFAULT_HOST:
        base = f"{ns}/{model}"
    else:
        base = f"{host}/{ns}/{model}"
    return f"{base}:{tag}"


def canonical(name: str) -> str:
    return short_name(*parse_name(name))


def manifest_path(name: str, base: Optional[Path] = None) -> Path:
    host, ns, model, tag = parse_name(name)
    return store_root(base) / "manifests" / host / ns / model / tag


def blob_path(digest: str, base: Optional[Path] = None) -> Path:
    if not re.fullmatch(r"sha256[:-][0-9a-f]{64}", digest):
        raise LocalAIError(f"not a blob digest: {digest}")
    return store_root(base) / "blobs" / digest.replace(":", "-")


# ---- blobs -------------------------------------------------------------------------------------------------------- #

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def put_bytes(data: bytes, base: Optional[Path] = None) -> dict:
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    p = blob_path(digest, base)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
    return {"digest": digest, "size": len(data)}


def put_file(src: Path, link: bool = True, base: Optional[Path] = None, digest: str = "") -> dict:
    """A file into the blob store: hard-linked when on the same drive (no extra space), else copied."""
    digest = digest or sha256_file(src)
    p = blob_path(digest, base)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        done = False
        if link:
            try:
                os.link(src, p)
                done = True
            except OSError:
                pass
        if not done:
            tmp = p.with_suffix(".tmp")
            shutil.copyfile(src, tmp)
            os.replace(tmp, p)
    return {"digest": digest, "size": p.stat().st_size}


# ---- manifests ---------------------------------------------------------------------------------------------------- #

def read_manifest(name: str, base: Optional[Path] = None) -> dict:
    p = manifest_path(name, base)
    if not p.is_file():
        raise LocalAIError(f"model {canonical(name)!r} not found, try pulling it first")
    return json.loads(p.read_text(encoding="utf-8"))


def write_manifest(name: str, layers: list[dict], config: dict, base: Optional[Path] = None) -> dict:
    cfg = put_bytes(json.dumps(config, separators=(",", ":")).encode(), base)
    man = {"schemaVersion": 2, "mediaType": MANIFEST_MT,
           "config": {"mediaType": CONFIG_MT, "digest": cfg["digest"], "size": cfg["size"]}, "layers": layers}
    p = manifest_path(name, base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(man, separators=(",", ":")), encoding="utf-8")
    return man


def config_for(weights: Path) -> dict:
    try:
        s = gguf.summary(gguf.read(weights, tensors=True))
    except (LocalAIError, OSError):
        s = {}
    fam = s.get("architecture") or "unknown"
    return {"model_format": "gguf", "model_family": fam, "model_families": [fam], "model_type": s.get("parameter_size", ""),
            "file_type": s.get("quantization", ""), "architecture": "amd64", "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": []}}


def _refs() -> dict:
    p = store_root() / "abp-references.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_refs(r: dict) -> None:
    p = store_root() / "abp-references.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(r, indent=1), encoding="utf-8")
    os.replace(tmp, p)


# ---- the model record --------------------------------------------------------------------------------------------- #

def resolve(name: str) -> dict:
    """Everything needed to run a model: weights path, projector, adapters, template, system, params, messages."""
    cname = canonical(name)
    ref = _refs().get(cname)
    if ref:
        w = Path(ref["weights"])
        if not w.is_file():
            raise LocalAIError(f"{cname} refers to {w}, which is no longer there")
        return {"name": cname, "source": "referenced", "origin": ref.get("origin", ""), "weights": str(w),
                "projector": ref.get("projector"), "adapters": ref.get("adapters", []), "template": ref.get("template"),
                "system": ref.get("system"), "params": ref.get("params", {}), "messages": ref.get("messages", []),
                "license": ref.get("license"), "modified": ref.get("added"), "digest": ref.get("digest", "")}
    for base, origin in [(None, "")] + [(Path(s["path"]), s["name"]) for s in extra_stores()]:
        try:
            man = read_manifest(cname, base)
        except LocalAIError:
            continue
        out: dict[str, Any] = {"name": cname, "source": "store" if base is None else f"{origin} (read in place)", "adapters": [],
                               "params": {}, "messages": [], "digest": hashlib.sha256(json.dumps(man).encode()).hexdigest(),
                               "modified": manifest_path(cname, base).stat().st_mtime}
        for layer in man.get("layers", []):
            mt, p = layer.get("mediaType", ""), blob_path(layer["digest"], base)
            if mt == MT["model"]:
                out["weights"] = str(p)
            elif mt == MT["projector"]:
                out["projector"] = str(p)
            elif mt == MT["adapter"]:
                out["adapters"].append(str(p))
            elif mt == MT["template"]:
                out["template"] = p.read_text(encoding="utf-8")
            elif mt == MT["system"]:
                out["system"] = p.read_text(encoding="utf-8")
            elif mt == MT["params"]:
                out["params"] = json.loads(p.read_text(encoding="utf-8"))
            elif mt == MT["messages"]:
                out["messages"] = json.loads(p.read_text(encoding="utf-8"))
            elif mt == MT["license"]:
                out["license"] = p.read_text(encoding="utf-8")
        if not out.get("weights") or not Path(out["weights"]).exists():
            raise LocalAIError(f"{cname}'s weights are missing from the store (pull it again)")
        return out
    raise LocalAIError(f"model {cname!r} not found, try pulling it first")


def _walk_manifests(base: Path) -> list[tuple[str, Path]]:
    out = []
    root = base / "manifests"
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        if p.is_file() and not p.name.endswith(".tmp"):
            rel = p.relative_to(root).parts
            if len(rel) >= 4:
                host, model, tag = rel[0], rel[-2], rel[-1]
                ns = "/".join(rel[1:-2])
                try:
                    out.append((short_name(host, ns, model, tag), p))
                except LocalAIError:
                    continue
    return out


def listing() -> list[dict]:
    """Every model ABP can run: its own store, references, and extra stores read in place."""
    out, seen = [], set()
    for base, origin in [(None, "store")] + [(Path(s["path"]), s["name"]) for s in extra_stores()]:
        for name, p in _walk_manifests(store_root(base)):
            if name in seen:
                continue
            try:
                man = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            size = sum(int(l.get("size", 0)) for l in man.get("layers", []))
            cfg = {}
            try:
                cfg = json.loads(blob_path(man["config"]["digest"], store_root(base)).read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError, LocalAIError):
                pass
            if not cfg.get("file_type") or not cfg.get("model_type"):     # e.g. hf.co configs: read the weights' header
                w = next((l for l in man.get("layers", []) if l.get("mediaType") == MT["model"]), None)
                try:
                    s = gguf.summary(gguf.read(blob_path(w["digest"], store_root(base)))) if w else {}
                    cfg = {**cfg, "file_type": s.get("quantization", ""), "model_type": s.get("parameter_size", ""),
                           "model_family": cfg.get("model_family") or s.get("architecture", "")}
                except (OSError, LocalAIError):
                    pass
            seen.add(name)
            out.append({"name": name, "model": name, "size": size, "modified_at": _iso(p.stat().st_mtime),
                        "digest": hashlib.sha256(p.read_bytes()).hexdigest(), "source": origin,
                        "details": {"format": cfg.get("model_format", "gguf"), "family": cfg.get("model_family", ""),
                                    "families": cfg.get("model_families"), "parameter_size": cfg.get("model_type", ""),
                                    "quantization_level": cfg.get("file_type", "")}})
    for name, r in _refs().items():
        if name in seen:
            continue
        w = Path(r["weights"])
        out.append({"name": name, "model": name, "size": w.stat().st_size if w.exists() else 0, "modified_at": _iso(r.get("added", 0)),
                    "digest": r.get("digest", ""), "source": f"referenced: {r.get('origin', '')}", "missing": not w.exists(),
                    "details": {"format": "gguf", "family": r.get("family", ""), "families": [r.get("family", "")],
                                "parameter_size": r.get("parameter_size", ""), "quantization_level": r.get("quantization", "")}})
    return sorted(out, key=lambda m: m["name"])


def _iso(t: float) -> str:
    """RFC 3339 with the local offset, as Ollama writes times (its clients parse them strictly)."""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(t or 0).astimezone().isoformat()


def add_reference(name: str, weights: str, origin: str = "", projector: str = "", **extra) -> dict:
    w = Path(weights)
    if not w.is_file():
        raise LocalAIError(f"{weights} is not a file")
    s = gguf.summary(gguf.read(w))
    cname = canonical(name)
    rec = {"weights": str(w), "origin": origin or str(w.parent), "added": time.time(), "family": s["architecture"],
           "parameter_size": s["parameter_size"], "quantization": s["quantization"], **({"projector": projector} if projector else {}), **extra}
    r = _refs()
    r[cname] = rec
    _save_refs(r)
    return {"name": cname, **rec}


def import_file(name: str, weights: str, projector: str = "", link: bool = True, template: str = "", system: str = "",
                params: Optional[dict] = None) -> dict:
    """Bring a GGUF into the store under a name (hard link when possible, else a copy)."""
    w = Path(weights)
    if not w.is_file():
        raise LocalAIError(f"{weights} is not a file")
    gguf.read(w, tensors=False)                       # it must be a GGUF
    layers = [{"mediaType": MT["model"], **put_file(w, link)}]
    if projector:
        layers.append({"mediaType": MT["projector"], **put_file(Path(projector), link)})
    for kind, text in (("template", template), ("system", system)):
        if text:
            layers.append({"mediaType": MT[kind], **put_bytes(text.encode())})
    if params:
        layers.append({"mediaType": MT["params"], **put_bytes(json.dumps(params).encode())})
    write_manifest(name, layers, config_for(w))
    r = _refs()
    if r.pop(canonical(name), None):
        _save_refs(r)
    return {"name": canonical(name), "layers": len(layers)}


def copy(src: str, dst: str) -> None:
    s = resolve(src)
    if s["source"].startswith("referenced"):
        r = _refs()
        r[canonical(dst)] = r[canonical(src)]
        _save_refs(r)
        return
    p = manifest_path(src)
    if not p.exists():                                   # from an extra store: import it under the new name
        import_file(dst, s["weights"], s.get("projector") or "", template=s.get("template") or "", system=s.get("system") or "",
                    params=s.get("params") or None)
        return
    d = manifest_path(dst)
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(p, d)


def delete(name: str) -> bool:
    cname = canonical(name)
    r = _refs()
    if r.pop(cname, None):
        _save_refs(r)
        return True
    p = manifest_path(cname)
    if not p.exists():
        raise LocalAIError(f"model {cname!r} not found")
    p.unlink()
    prune()
    return True


def prune() -> int:
    """Delete blobs no manifest uses (after deletes); returns how many."""
    root = store_root()
    used = set()
    for _n, p in _walk_manifests(root):
        try:
            man = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        used.add(man["config"]["digest"].replace(":", "-"))
        used.update(l["digest"].replace(":", "-") for l in man.get("layers", []))
    n = 0
    for b in (root / "blobs").glob("sha256-*"):
        if b.name not in used and not b.name.endswith((".partial", ".tmp")):
            b.unlink()
            n += 1
    return n


# ---- extra stores (other programs' model folders, read in place) -------------------------------------------------- #

def extra_stores() -> list[dict]:
    p = home() / "stores.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def set_extra_stores(stores: list[dict]) -> list[dict]:
    clean = []
    for s in stores:
        path = Path(s["path"])
        if not (path / "manifests").is_dir():
            raise LocalAIError(f"{path} is not an Ollama-format model folder (no manifests/ inside)")
        clean.append({"name": s.get("name") or path.name, "path": str(path)})
    (home() / "stores.json").write_text(json.dumps(clean, indent=1), encoding="utf-8")
    return clean

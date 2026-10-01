"""Downloading models into ABP's store.

    pull("qwen2.5:0.5b")                       Ollama's registry (registry.ollama.ai), its OCI-style API
    pull("hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M")   Hugging Face serves the same API for GGUF repositories
    pull_url("https://.../model.gguf", "name:tag")         any GGUF by address

Every blob is downloaded to `<digest>-partial` (continued with a Range request after an interruption), checked
against its SHA-256 digest, then renamed into place; blobs already present are skipped (shared layers, re-pulls).
progress(status_dict) gets Ollama's own progress messages, so /api/pull streams exactly what Ollama clients expect.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Callable

import httpx

from bot.localai import models
from bot.localai.paths import LocalAIError

Progress = Callable[[dict], None]
UA = {"User-Agent": "ABP-LocalAI/1 (ollama-compatible)"}


def _registry(host: str) -> str:
    return "https://registry.ollama.ai" if host == models.DEFAULT_HOST else f"https://{host}"


def _download(url: str, dest: Path, digest: str, size: int, progress: Progress, client: httpx.Client) -> None:
    part = dest.with_name(dest.name + "-partial")
    have = part.stat().st_size if part.exists() else 0
    if have > size > 0:
        part.unlink()
        have = 0
    h = hashlib.sha256()
    if have:
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(8 << 20), b""):
                h.update(block)
    short = digest.split(":")[-1][:12]
    last = 0.0
    for attempt in range(6):
        try:
            hdr = {**UA, **({"Range": f"bytes={have}-"} if have else {})}
            with client.stream("GET", url, headers=hdr, follow_redirects=True) as r:
                if r.status_code == 416:
                    break
                if r.status_code >= 400:
                    raise LocalAIError(f"download of {short} failed: HTTP {r.status_code}")
                if have and r.status_code == 200:          # the server ignored Range: start over
                    have, h = 0, hashlib.sha256()
                with open(part, "ab" if have else "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        h.update(chunk)
                        have += len(chunk)
                        if time.monotonic() - last > 0.25:
                            progress({"status": f"pulling {short}", "digest": digest, "total": size, "completed": have})
                            last = time.monotonic()
            break
        except (httpx.HTTPError, OSError) as e:
            if attempt == 5:
                raise LocalAIError(f"download of {short} kept failing: {e}") from e
            time.sleep(2 * (attempt + 1))
    progress({"status": f"pulling {short}", "digest": digest, "total": size, "completed": have})
    progress({"status": "verifying sha256 digest"})
    got = "sha256:" + h.hexdigest()
    if got != digest:
        part.unlink(missing_ok=True)
        raise LocalAIError(f"digest mismatch for {short}: the download was damaged; pull again")
    os.replace(part, dest)


def pull(name: str, progress: Progress = lambda s: None, insecure: bool = False) -> dict:
    host, ns, model, tag = models.parse_name(name)
    base = _registry(host)
    accept = {"Accept": f"{models.MANIFEST_MT}, application/vnd.oci.image.manifest.v1+json", **UA}
    progress({"status": "pulling manifest"})
    with httpx.Client(timeout=httpx.Timeout(60, connect=20), verify=not insecure) as c:
        r = c.get(f"{base}/v2/{ns}/{model}/manifests/{tag}", headers=accept, follow_redirects=True)
        if r.status_code == 404:
            raise LocalAIError(f"pull model manifest: file does not exist ({models.canonical(name)} is not in {host})")
        if r.status_code >= 400:
            raise LocalAIError(f"pull model manifest: HTTP {r.status_code} {r.text[:200]}")
        man = r.json()
        for item in [man["config"]] + man.get("layers", []):
            dest = models.blob_path(item["digest"])
            if dest.exists() and dest.stat().st_size == int(item.get("size", -1)):
                progress({"status": f"pulling {item['digest'].split(':')[-1][:12]}", "digest": item["digest"],
                          "total": item.get("size"), "completed": item.get("size")})
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            _download(f"{base}/v2/{ns}/{model}/blobs/{item['digest']}", dest, item["digest"], int(item.get("size", 0)), progress, c)
    progress({"status": "writing manifest"})
    p = models.manifest_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(r.content)
    progress({"status": "success"})
    return {"name": models.canonical(name), "layers": len(man.get("layers", [])),
            "size": sum(int(l.get("size", 0)) for l in man.get("layers", []))}


def pull_url(url: str, name: str, progress: Progress = lambda s: None, sha256: str = "") -> dict:
    """A GGUF by address: downloaded (resumable), then imported into the store under `name`."""
    tmp_dir = models.store_root() / "downloads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / (url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "model.gguf")
    part = dest.with_name(dest.name + "-partial")
    have = part.stat().st_size if part.exists() else 0
    with httpx.Client(timeout=httpx.Timeout(60, connect=20)) as c:
        with c.stream("GET", url, headers={**UA, **({"Range": f"bytes={have}-"} if have else {})}, follow_redirects=True) as r:
            if r.status_code >= 400 and r.status_code != 416:
                raise LocalAIError(f"download failed: HTTP {r.status_code}")
            total = int(r.headers.get("content-length", 0)) + (have if r.status_code == 206 else 0)
            if have and r.status_code == 200:
                have = 0
            last = 0.0
            with open(part, "ab" if have else "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    have += len(chunk)
                    if time.monotonic() - last > 0.25:
                        progress({"status": f"downloading {dest.name}", "total": total, "completed": have})
                        last = time.monotonic()
    os.replace(part, dest)
    progress({"status": "verifying sha256 digest"})
    digest = models.sha256_file(dest)
    if sha256 and digest.split(":")[-1] != sha256.lower():
        dest.unlink()
        raise LocalAIError("the download does not match the expected SHA-256")
    res = models.import_file(name, str(dest), link=True)
    try:
        dest.unlink()             # it now lives in blobs/ (hard link or copy)
    except OSError:
        pass
    progress({"status": "success"})
    return {**res, "digest": digest}

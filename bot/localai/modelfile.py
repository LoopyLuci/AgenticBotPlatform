'''Ollama's Modelfile, and creating models from one.

    FROM <model name | path to a .gguf | path to a folder of .gguf>
    PARAMETER <name> <value>          temperature, top_k, top_p, min_p, num_ctx, num_predict, repeat_penalty,
                                      repeat_last_n, seed, stop (repeatable), num_gpu, mirostat...
    TEMPLATE """..."""                the prompt template (kept; the model's own chat template is what runs)
    SYSTEM """..."""                  the default system prompt
    ADAPTER <path to a LoRA .gguf>
    MESSAGE <user|assistant|system> <text>     example conversation turns, put before every chat
    LICENSE """..."""

create(name, text) builds the new model's manifest on top of FROM's layers (the weights are shared, never copied).
'''
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from bot.localai import models
from bot.localai.paths import LocalAIError

_INT = {"num_ctx", "num_predict", "top_k", "repeat_last_n", "seed", "num_gpu", "mirostat", "num_keep", "num_batch", "num_thread"}
_FLOAT = {"temperature", "top_p", "min_p", "repeat_penalty", "presence_penalty", "frequency_penalty", "mirostat_tau",
          "mirostat_eta", "typical_p", "tfs_z"}


def parse(text: str) -> list[tuple[str, str]]:
    out = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        cmd, _, rest = line.partition(" ")
        cmd = cmd.upper()
        rest = rest.strip()
        if rest.startswith('"""'):
            body = rest[3:]
            if body.endswith('"""') and len(body) >= 3:
                rest = body[:-3]
            else:
                parts = [body]
                while i < len(lines):
                    ln = lines[i]
                    i += 1
                    if ln.rstrip().endswith('"""'):
                        parts.append(ln.rstrip()[:-3])
                        break
                    parts.append(ln)
                rest = "\n".join(parts)
        elif len(rest) >= 2 and rest[0] == rest[-1] == '"':
            rest = rest[1:-1]
        if cmd not in ("FROM", "PARAMETER", "TEMPLATE", "SYSTEM", "ADAPTER", "MESSAGE", "LICENSE", "REQUIRES"):
            raise LocalAIError(f"unknown Modelfile instruction {cmd}")
        out.append((cmd, rest))
    if not any(c == "FROM" for c, _ in out):
        raise LocalAIError("a Modelfile needs a FROM line")
    return out


def _value(name: str, raw: str):
    if name in _INT:
        return int(raw)
    if name in _FLOAT:
        return float(raw)
    return raw


def _is_gguf(p: Path) -> bool:
    with open(p, "rb") as f:
        return f.read(4) == b"GGUF"


def create(name: str, text: str, progress: Callable[[dict], None] = lambda s: None, base_dir: Optional[Path] = None) -> dict:
    ins = parse(text)
    params: dict = {}
    layers: list[dict] = []
    messages: list[dict] = []
    src = next(r for c, r in ins if c == "FROM")
    base_dir = base_dir or Path.cwd()
    progress({"status": "reading model metadata"})
    p = Path(src) if Path(src).is_absolute() else base_dir / src
    if p.is_dir():
        ggufs = sorted(p.glob("*.gguf"), key=lambda x: -x.stat().st_size)
        if not ggufs:
            raise LocalAIError(f"{p} holds no .gguf file")
        p = ggufs[0]
    if p.is_file() and (p.suffix.lower() == ".gguf" or _is_gguf(p)):     # uploaded blobs have no extension
        progress({"status": "creating model layer"})
        layers.append({"mediaType": models.MT["model"], **models.put_file(p)})
        config = models.config_for(p)
        inherited = {}
    else:
        base = models.resolve(src)
        man = models.read_manifest(src) if models.manifest_path(src).exists() else None
        if man:
            layers = [l for l in man["layers"] if l["mediaType"] in (models.MT["model"], models.MT["projector"], models.MT["adapter"])]
        else:                                               # a referenced or extra-store model: bring its file in
            layers = [{"mediaType": models.MT["model"], **models.put_file(Path(base["weights"]))}]
        config = models.config_for(Path(base["weights"]))
        inherited = {"template": base.get("template"), "system": base.get("system"), "license": base.get("license")}
        params.update(base.get("params") or {})
        messages = list(base.get("messages") or [])
    texts = dict(inherited)
    for cmd, rest in ins:
        if cmd == "PARAMETER":
            k, _, v = rest.partition(" ")
            k, v = k.strip(), v.strip()
            if k == "stop":
                params.setdefault("stop", [])
                if not isinstance(params["stop"], list):
                    params["stop"] = [params["stop"]]
                params["stop"].append(v.strip('"'))
            else:
                try:
                    params[k] = _value(k, v)
                except ValueError as e:
                    raise LocalAIError(f"PARAMETER {k}: {v!r} is not a valid value") from e
        elif cmd in ("TEMPLATE", "SYSTEM", "LICENSE"):
            texts[cmd.lower()] = rest
        elif cmd == "ADAPTER":
            ap = Path(rest) if Path(rest).is_absolute() else base_dir / rest
            if not ap.is_file():
                raise LocalAIError(f"ADAPTER {rest}: no such file")
            progress({"status": "creating adapter layer"})
            layers.append({"mediaType": models.MT["adapter"], **models.put_file(ap)})
        elif cmd == "MESSAGE":
            role, _, content = rest.partition(" ")
            if role not in ("system", "user", "assistant"):
                raise LocalAIError(f"MESSAGE role {role!r} is not system, user or assistant")
            messages.append({"role": role, "content": content})
    for kind in ("template", "system", "license"):
        if texts.get(kind):
            layers.append({"mediaType": models.MT[kind], **models.put_bytes(texts[kind].encode())})
    if params:
        layers.append({"mediaType": models.MT["params"], **models.put_bytes(json.dumps(params).encode())})
    if messages:
        layers.append({"mediaType": models.MT["messages"], **models.put_bytes(json.dumps(messages).encode())})
    progress({"status": "writing manifest"})
    models.write_manifest(name, layers, config)
    progress({"status": "success"})
    return {"name": models.canonical(name), "layers": len(layers), "parameters": params}


def render(rec: dict) -> str:
    """The Modelfile of a model (what `ollama show --modelfile` prints)."""
    out = ["# Modelfile generated by ABP", f"FROM {rec['weights']}"]
    if rec.get("projector"):
        out.append(f"# projector: {rec['projector']}")
    for a in rec.get("adapters") or []:
        out.append(f"ADAPTER {a}")
    if rec.get("template"):
        out.append(f'TEMPLATE """{rec["template"]}"""')
    if rec.get("system"):
        out.append(f'SYSTEM """{rec["system"]}"""')
    for k, v in (rec.get("params") or {}).items():
        for item in (v if isinstance(v, list) else [v]):
            out.append(f"PARAMETER {k} {json.dumps(item) if isinstance(item, str) and ' ' in item else item}")
    for m in rec.get("messages") or []:
        out.append(f"MESSAGE {m['role']} {m['content']}")
    if rec.get("license"):
        out.append(f'LICENSE """{rec["license"]}"""')
    return "\n".join(out) + "\n"


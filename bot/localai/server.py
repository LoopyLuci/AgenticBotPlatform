"""ABP's local model server, Ollama-compatible:  python -m bot.localai.server [--port 11436] [--bind 127.0.0.1]

Anything built for Ollama talks to it unchanged (point OLLAMA_HOST / the base URL at it; or run it on 11434 when
Ollama itself is not running). The OpenAI-compatible routes sit on the same port under /v1.

Ollama API
    POST /api/generate     prompt (+ system, images, format, options, raw, suffix for fill-in-the-middle, think),
                           streamed as NDJSON by default; an empty prompt only loads (keep_alive 0 unloads)
    POST /api/chat         messages (+ tools, images, format: "json" or a JSON schema, options, think); tool calls and
                           thinking come back as message.tool_calls / message.thinking
    POST /api/embed        input: a string or a list -> L2-normalised embeddings;  POST /api/embeddings (legacy)
    GET  /api/tags         the models;  GET /api/ps the loaded ones;  POST /api/show  details, Modelfile, capabilities
    POST /api/pull         from Ollama's registry or hf.co, progress streamed;  POST /api/push  (not offered)
    POST /api/create       from a Modelfile ("modelfile") or fields (from, files, adapters, system, template,
                           parameters, messages, quantize);  HEAD/POST /api/blobs/<digest>
    POST /api/copy  DELETE /api/delete  GET /api/version  GET /
OpenAI API
    GET /v1/models   POST /v1/chat/completions   POST /v1/completions   POST /v1/embeddings
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from bot.localai import engine, gguf, modelfile, models, pull
from bot.localai.paths import LocalAIError

VERSION = "0.12.6"          # the Ollama API level this server speaks (clients check it); X-ABP-LocalAI says who it is
_client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10))
OPTION_MAP = {"temperature": "temperature", "top_p": "top_p", "top_k": "top_k", "min_p": "min_p", "seed": "seed",
              "repeat_penalty": "repeat_penalty", "repeat_last_n": "repeat_last_n", "presence_penalty": "presence_penalty",
              "frequency_penalty": "frequency_penalty", "typical_p": "typical_p", "mirostat": "mirostat",
              "mirostat_tau": "mirostat_tau", "mirostat_eta": "mirostat_eta", "stop": "stop", "num_keep": "n_keep"}


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status, headers={"X-ABP-LocalAI": "1"})


async def _load(name: str, options: dict, keep_alive, embedding: bool = False) -> engine.Runner:
    ka = engine.parse_keep_alive(keep_alive)
    load_opts = {k: v for k, v in (options or {}).items() if k in ("num_ctx", "num_gpu", "seed")}
    return await asyncio.to_thread(engine.load, name, load_opts, ka, embedding)


def _sampling(rec_params: dict, options: dict) -> dict:
    out = {}
    merged = {**(rec_params or {}), **(options or {})}
    for k, v in merged.items():
        if k in OPTION_MAP:
            out[OPTION_MAP[k]] = v
    if "num_predict" in merged and merged["num_predict"] not in (None, -1, -2):
        out["max_tokens"] = int(merged["num_predict"])
    return out


def _format(fmt) -> Optional[dict]:
    if not fmt:
        return None
    if fmt == "json":
        return {"type": "json_object"}
    if isinstance(fmt, dict):
        return {"type": "json_schema", "json_schema": {"name": "output", "schema": fmt, "strict": True}}
    raise LocalAIError('format is "json" or a JSON schema')


MEMORY_SUFFIX = "+memory"


def _memory(request: Request, b: dict, query: str) -> str:
    """ABP's shared memory (bot/memoryfabric) for a client that asks for it: a model named "<model>+memory" or the
    X-ABP-Memory header (X-ABP-Instance adds that bot's own memories). The suffix is taken off the model name."""
    model = str(b.get("model") or "")
    want = model.endswith(MEMORY_SUFFIX) or request.headers.get("x-abp-memory", "").lower() in ("1", "true", "yes", "on")
    if model.endswith(MEMORY_SUFFIX):
        b["model"] = model[: -len(MEMORY_SUFFIX)]
    if not want:
        return ""
    try:
        from bot.memoryfabric import store
        iid = request.headers.get("x-abp-instance")
        return store.memory_block(int(iid) if iid and iid.isdigit() else None, query)
    except Exception:  # noqa: BLE001 - memory never fails a request
        return ""


def _last_user(msgs: list[dict]) -> str:
    for m in reversed(msgs or []):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


def _with_memory(msgs: list[dict], block: str) -> list[dict]:
    """The memory block in the conversation's one system message (added at the start when there is none)."""
    if not block:
        return msgs
    if msgs and msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str):
        return [{**msgs[0], "content": f"{msgs[0]['content']}\n\n{block}"}] + msgs[1:]
    return [{"role": "system", "content": block}] + list(msgs)


def _messages(rec: dict, msgs: list[dict], system: Optional[str] = None) -> list[dict]:
    out = []
    if system or (rec.get("system") and not any(m.get("role") == "system" for m in msgs)):
        out.append({"role": "system", "content": system or rec["system"]})
    out += [dict(m) for m in rec.get("messages") or []]
    for m in msgs:
        mm: dict[str, Any] = {"role": m.get("role", "user")}
        imgs = m.get("images") or []
        if imgs:
            parts = [{"type": "text", "text": m.get("content", "")}] if m.get("content") else []
            for im in imgs:
                url = im if str(im).startswith("data:") else f"data:image/png;base64,{im}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            mm["content"] = parts
        else:
            mm["content"] = m.get("content", "")
        if m.get("tool_calls"):
            mm["tool_calls"] = [{"id": tc.get("id") or f"call_{i}", "type": "function",
                                 "function": {"name": tc["function"]["name"],
                                              "arguments": tc["function"]["arguments"] if isinstance(tc["function"].get("arguments"), str)
                                              else json.dumps(tc["function"].get("arguments") or {})}}
                                for i, tc in enumerate(m["tool_calls"])]
        if m.get("role") == "tool":
            if m.get("tool_call_id"):
                mm["tool_call_id"] = m["tool_call_id"]
            if m.get("tool_name"):
                mm["name"] = m["tool_name"]
        if m.get("thinking") and m.get("role") == "assistant":
            mm["reasoning_content"] = m["thinking"]
        out.append(mm)
    return out


def _tool_calls(tcs: list[dict]) -> list[dict]:
    out = []
    for tc in tcs:
        fn = tc.get("function") or {}
        args = fn.get("arguments") or "{}"
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except ValueError:
            args = {"_raw": args}
        out.append({"function": {"name": fn.get("name", ""), "arguments": args}})
    return out


async def _chat_stream(r: engine.Runner, body: dict, model: str, t0: float, load_s: float, field: str) -> AsyncIterator[bytes]:
    """llama-server's SSE (OpenAI deltas) -> Ollama's NDJSON chunks."""
    r.busy += 1
    tool_acc: dict[int, dict] = {}
    timings, done_reason = {}, "stop"
    try:
        async with _client.stream("POST", f"{r.url}/v1/chat/completions", json={**body, "stream": True, "timings_per_token": False}) as resp:
            if resp.status_code >= 400:
                text = (await resp.aread()).decode(errors="replace")
                yield (json.dumps({"error": f"engine: {text[:500]}"}) + "\n").encode()
                return
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    j = json.loads(data)
                except ValueError:
                    continue
                if j.get("timings"):
                    timings = j["timings"]
                for ch in j.get("choices") or []:
                    d = ch.get("delta") or {}
                    if ch.get("finish_reason"):
                        done_reason = "stop" if ch["finish_reason"] in ("stop", "tool_calls") else ch["finish_reason"]
                    for tc in d.get("tool_calls") or []:
                        acc = tool_acc.setdefault(tc.get("index", 0), {"function": {"name": "", "arguments": ""}})
                        f = tc.get("function") or {}
                        acc["function"]["name"] += f.get("name") or ""
                        acc["function"]["arguments"] += f.get("arguments") or ""
                    piece, think = d.get("content") or "", d.get("reasoning_content") or ""
                    if piece or think:
                        chunk: dict[str, Any] = {"model": model, "created_at": _now(), "done": False}
                        if field == "message":
                            msg = {"role": "assistant", "content": piece}
                            if think:
                                msg["thinking"] = think
                            chunk["message"] = msg
                        else:
                            chunk["response"] = piece
                            if think:
                                chunk["thinking"] = think
                        yield (json.dumps(chunk) + "\n").encode()
        if tool_acc and field == "message":
            yield (json.dumps({"model": model, "created_at": _now(), "done": False, "message": {
                "role": "assistant", "content": "", "tool_calls": _tool_calls([tool_acc[k] for k in sorted(tool_acc)])}}) + "\n").encode()
        final = {"model": model, "created_at": _now(), "done": True, "done_reason": done_reason, **_durations(t0, load_s, timings)}
        if field == "message":
            final["message"] = {"role": "assistant", "content": ""}
        else:
            final["response"] = ""
            final["context"] = []
        yield (json.dumps(final) + "\n").encode()
    finally:
        r.busy -= 1
        r.last_used = time.time()


def _durations(t0: float, load_s: float, timings: dict) -> dict:
    ns = lambda s: int(s * 1e9)  # noqa: E731
    return {"total_duration": ns(time.time() - t0), "load_duration": ns(load_s),
            "prompt_eval_count": int(timings.get("prompt_n", 0)), "prompt_eval_duration": ns(timings.get("prompt_ms", 0) / 1000),
            "eval_count": int(timings.get("predicted_n", 0)), "eval_duration": ns(timings.get("predicted_ms", 0) / 1000)}


async def _chat_once(r: engine.Runner, body: dict, model: str, t0: float, load_s: float, field: str) -> dict:
    r.busy += 1
    try:
        resp = await _client.post(f"{r.url}/v1/chat/completions", json={**body, "stream": False})
    finally:
        r.busy -= 1
        r.last_used = time.time()
    if resp.status_code >= 400:
        raise LocalAIError(f"engine: {resp.text[:500]}")
    j = resp.json()
    ch = (j.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    out: dict[str, Any] = {"model": model, "created_at": _now(), "done": True,
                           "done_reason": "stop" if ch.get("finish_reason") in ("stop", "tool_calls", None) else ch.get("finish_reason"),
                           **_durations(t0, load_s, j.get("timings") or {})}
    if field == "message":
        m = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("reasoning_content"):
            m["thinking"] = msg["reasoning_content"]
        if msg.get("tool_calls"):
            m["tool_calls"] = _tool_calls(msg["tool_calls"])
        out["message"] = m
    else:
        out["response"] = msg.get("content") or ""
        if msg.get("reasoning_content"):
            out["thinking"] = msg["reasoning_content"]
        out["context"] = []
    return out


def _ndjson(gen) -> StreamingResponse:
    return StreamingResponse(gen, media_type="application/x-ndjson", headers={"X-ABP-LocalAI": "1"})


async def chat(request: Request):
    try:
        b = await request.json()
        block = await asyncio.to_thread(_memory, request, b, _last_user(b.get("messages") or []))
        model = b.get("model") or ""
        rec = await asyncio.to_thread(models.resolve, model)
        t0 = time.time()
        msgs = b.get("messages") or []
        if not msgs:                                        # load / unload only
            ka = engine.parse_keep_alive(b.get("keep_alive"))
            if ka == 0:
                await asyncio.to_thread(engine.unload, model)
                return JSONResponse({"model": rec["name"], "created_at": _now(), "message": {"role": "assistant", "content": ""},
                                     "done_reason": "unload", "done": True})
            await _load(model, b.get("options") or {}, b.get("keep_alive"))
            return JSONResponse({"model": rec["name"], "created_at": _now(), "message": {"role": "assistant", "content": ""},
                                 "done_reason": "load", "done": True})
        before = time.time()
        r = await _load(model, b.get("options") or {}, b.get("keep_alive"))
        load_s = getattr(r, "load_seconds", 0.0) if r.started >= before else 0.0
        body: dict[str, Any] = {"messages": _with_memory(_messages(rec, msgs), block), **_sampling(rec.get("params") or {}, b.get("options") or {})}
        if b.get("tools"):
            body["tools"] = b["tools"]
        rf = _format(b.get("format"))
        if rf:
            body["response_format"] = rf
        if b.get("think") is not None:
            body["chat_template_kwargs"] = {"enable_thinking": bool(b["think"])}
            if not b["think"]:
                body["reasoning_format"] = "none"
        if b.get("stream", True):
            return _ndjson(_chat_stream(r, body, rec["name"], t0, load_s, "message"))
        return JSONResponse(await _chat_once(r, body, rec["name"], t0, load_s, "message"))
    except LocalAIError as e:
        return _err(str(e), 404 if "not found" in str(e) else 400)


async def generate(request: Request):
    try:
        b = await request.json()
        block = await asyncio.to_thread(_memory, request, b, str(b.get("prompt") or ""))
        model = b.get("model") or ""
        rec = await asyncio.to_thread(models.resolve, model)
        t0 = time.time()
        prompt = b.get("prompt") or ""
        if not prompt and not b.get("suffix") and not b.get("images"):
            ka = engine.parse_keep_alive(b.get("keep_alive"))
            if ka == 0:
                await asyncio.to_thread(engine.unload, model)
                return JSONResponse({"model": rec["name"], "created_at": _now(), "response": "", "done": True, "done_reason": "unload"})
            await _load(model, b.get("options") or {}, b.get("keep_alive"))
            return JSONResponse({"model": rec["name"], "created_at": _now(), "response": "", "done": True, "done_reason": "load"})
        before = time.time()
        r = await _load(model, b.get("options") or {}, b.get("keep_alive"))
        load_s = getattr(r, "load_seconds", 0.0) if r.started >= before else 0.0
        samp = _sampling(rec.get("params") or {}, b.get("options") or {})
        if b.get("raw") or b.get("suffix"):                  # straight to the model: raw text, or fill-in-the-middle
            path = "/infill" if b.get("suffix") else "/completion"
            body = {"prompt": prompt, "n_predict": samp.pop("max_tokens", -1), **samp, "stream": False}
            if b.get("suffix"):
                body = {"input_prefix": prompt, "input_suffix": b["suffix"], **{k: v for k, v in body.items() if k != "prompt"}}
            r.busy += 1
            try:
                resp = await _client.post(f"{r.url}{path}", json=body)
            finally:
                r.busy -= 1
            if resp.status_code >= 400:
                raise LocalAIError(f"engine: {resp.text[:500]}")
            j = resp.json()
            out = {"model": rec["name"], "created_at": _now(), "response": j.get("content", ""), "done": True, "done_reason": "stop",
                   "context": [], **_durations(t0, load_s, j.get("timings") or {})}
            if b.get("stream", True):
                return _ndjson(iter([(json.dumps(out) + "\n").encode()]))
            return JSONResponse(out)
        msg = {"role": "user", "content": prompt, "images": b.get("images") or []}
        body = {"messages": _with_memory(_messages(rec, [msg], b.get("system")), block), **samp}
        rf = _format(b.get("format"))
        if rf:
            body["response_format"] = rf
        if b.get("think") is not None:
            body["chat_template_kwargs"] = {"enable_thinking": bool(b["think"])}
        if b.get("stream", True):
            return _ndjson(_chat_stream(r, body, rec["name"], t0, load_s, "response"))
        return JSONResponse(await _chat_once(r, body, rec["name"], t0, load_s, "response"))
    except LocalAIError as e:
        return _err(str(e), 404 if "not found" in str(e) else 400)


async def _embed(model: str, inputs: list[str], options: dict, keep_alive, truncate: bool = True) -> tuple[list, int, float]:
    t = time.time()
    r = await _load(model, options, keep_alive, embedding=True)
    load_s = time.time() - t
    r.busy += 1
    try:
        resp = await _client.post(f"{r.url}/v1/embeddings", json={"input": inputs})
    finally:
        r.busy -= 1
        r.last_used = time.time()
    if resp.status_code >= 400:
        raise LocalAIError(f"engine: {resp.text[:400]}")
    j = resp.json()
    vecs = [d["embedding"] for d in sorted(j["data"], key=lambda d: d["index"])]
    return vecs, int((j.get("usage") or {}).get("prompt_tokens", 0)), load_s


async def embed(request: Request):
    try:
        b = await request.json()
        inp = b.get("input")
        inputs = [inp] if isinstance(inp, str) else list(inp or [])
        if not inputs:
            return _err("input is empty")
        t0 = time.time()
        vecs, n, load_s = await _embed(b.get("model", ""), inputs, b.get("options") or {}, b.get("keep_alive"))
        out = []
        for v in vecs:
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            v = [x / norm for x in v]
            if b.get("dimensions"):
                v = v[: int(b["dimensions"])]
                norm = sum(x * x for x in v) ** 0.5 or 1.0
                v = [x / norm for x in v]
            out.append(v)
        return JSONResponse({"model": models.canonical(b["model"]), "embeddings": out, "total_duration": int((time.time() - t0) * 1e9),
                             "load_duration": int(load_s * 1e9), "prompt_eval_count": n})
    except LocalAIError as e:
        return _err(str(e), 404 if "not found" in str(e) else 400)


async def embeddings_legacy(request: Request):
    try:
        b = await request.json()
        vecs, _n, _l = await _embed(b.get("model", ""), [b.get("prompt", "")], b.get("options") or {}, b.get("keep_alive"))
        return JSONResponse({"embedding": vecs[0]})
    except LocalAIError as e:
        return _err(str(e), 404 if "not found" in str(e) else 400)


async def tags(request: Request):
    return JSONResponse({"models": await asyncio.to_thread(models.listing)})


async def ps(request: Request):
    return JSONResponse({"models": engine.running()})


def _capabilities(rec: dict, s: dict) -> list[str]:
    if s["embedding_model"]:
        return ["embedding"]
    caps = ["completion"]
    tmpl = (s.get("chat_template") or "") + (rec.get("template") or "")
    if "tools" in tmpl or "tool_call" in tmpl:
        caps.append("tools")
    if rec.get("projector"):
        caps.append("vision")
    if "think" in tmpl or "reasoning" in tmpl:
        caps.append("thinking")
    if "fim" in tmpl.lower() or s["architecture"] in ("starcoder2", "qwen2") and "coder" in (s.get("name") or "").lower():
        caps.append("insert")
    return caps


async def show(request: Request):
    try:
        b = await request.json()
        name = b.get("model") or b.get("name") or ""
        rec = await asyncio.to_thread(models.resolve, name)
        info = await asyncio.to_thread(gguf.read, rec["weights"], bool(b.get("verbose")))
        s = gguf.summary(info)
        meta = {k: v for k, v in info["meta"].items() if b.get("verbose") or not isinstance(v, (list, dict))}
        meta["general.parameter_count"] = info.get("parameters")
        params = "\n".join(f"{k:<30} {json.dumps(x) if isinstance(x, str) else x}" for k, v in (rec.get("params") or {}).items()
                           for x in (v if isinstance(v, list) else [v]))
        return JSONResponse({"modelfile": modelfile.render(rec), "parameters": params, "template": rec.get("template") or s.get("chat_template") or "",
                             "system": rec.get("system") or "", "license": rec.get("license") or s.get("license") or "",
                             "details": {"parent_model": "", "format": "gguf", "family": s["architecture"], "families": [s["architecture"]],
                                         "parameter_size": s["parameter_size"], "quantization_level": s["quantization"]},
                             "model_info": meta, "capabilities": _capabilities(rec, s),
                             "modified_at": models._iso(rec.get("modified") or 0),
                             "abp": {"source": rec["source"], "weights": rec["weights"]}})
    except LocalAIError as e:
        return _err(str(e), 404 if "not found" in str(e) else 400)


async def pull_ep(request: Request):
    b = await request.json()
    name = b.get("model") or b.get("name") or ""
    q: "asyncio.Queue[Optional[dict]]" = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def go():
        try:
            pull.pull(name, lambda s: loop.call_soon_threadsafe(q.put_nowait, s), bool(b.get("insecure")))
        except Exception as e:  # noqa: BLE001 - reported to the client as Ollama does
            loop.call_soon_threadsafe(q.put_nowait, {"error": str(e)})
        loop.call_soon_threadsafe(q.put_nowait, None)
    threading.Thread(target=go, daemon=True).start()
    if not b.get("stream", True):
        last: dict = {}
        while (item := await q.get()) is not None:
            last = item
        return JSONResponse(last, status_code=500 if "error" in last else 200)

    async def gen():
        while (item := await q.get()) is not None:
            yield (json.dumps(item) + "\n").encode()
    return _ndjson(gen())


async def push(request: Request):
    return _err("pushing models to a registry is not offered by ABP's runtime (pull, create, copy and delete are)", 501)


async def blobs(request: Request):
    digest = request.path_params["digest"]
    try:
        p = models.blob_path(digest)
    except LocalAIError as e:
        return _err(str(e))
    if request.method == "HEAD":
        return Response(status_code=200 if p.exists() else 404)
    tmp = p.with_name(p.name + ".upload")
    p.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    f = await asyncio.to_thread(open, tmp, "wb")            # file I/O off the event loop (blobs are gigabytes)
    try:
        async for chunk in request.stream():
            await asyncio.to_thread(f.write, chunk)
            h.update(chunk)
    finally:
        await asyncio.to_thread(f.close)
    if "sha256:" + h.hexdigest() != digest.replace("-", ":", 1):
        tmp.unlink()
        return _err("digest mismatch")
    os.replace(tmp, p)
    return Response(status_code=201)


def _quantize(src: str, qtype: str, progress) -> str:
    import subprocess
    from bot.localai.paths import CPU_THREADS, sub
    out = sub("tmp") / f"quant-{int(time.time())}-{qtype}.gguf"
    progress({"status": f"quantizing to {qtype} (CPU, {CPU_THREADS} threads)"})
    r = subprocess.run([engine.tool("llama-quantize"), src, str(out), qtype, str(CPU_THREADS)], capture_output=True, text=True,
                       creationflags=0x08000000 if sys.platform == "win32" else 0)
    if r.returncode != 0:
        raise LocalAIError(f"quantize failed: {(r.stderr or r.stdout)[-600:]}")
    return str(out)


async def create(request: Request):
    b = await request.json()
    name = b.get("model") or b.get("name") or ""
    q: "asyncio.Queue[Optional[dict]]" = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def say(s):
        loop.call_soon_threadsafe(q.put_nowait, s)

    def go():
        try:
            text = b.get("modelfile")
            if not text:
                lines = []
                src = b.get("from")
                files = b.get("files") or {}
                if files:
                    gguf_name = next((k for k in files if k.endswith(".gguf")), next(iter(files)))
                    src = str(models.blob_path(files[gguf_name]))
                if not src:
                    raise LocalAIError("create needs `from` (a model) or `files` (uploaded blobs)")
                if b.get("quantize"):
                    weights = models.resolve(src)["weights"] if not Path(src).exists() else src
                    src = _quantize(weights, b["quantize"].upper(), say)
                lines.append(f"FROM {src}")
                for _name, digest in (b.get("adapters") or {}).items():
                    lines.append(f"ADAPTER {models.blob_path(digest)}")
                for k in ("template", "system", "license"):
                    v = b.get(k)
                    if v:
                        lines.append(f'{k.upper()} """{v if isinstance(v, str) else chr(10).join(v)}"""')
                for k, v in (b.get("parameters") or {}).items():
                    for item in (v if isinstance(v, list) else [v]):
                        lines.append(f"PARAMETER {k} {item}")
                for m in b.get("messages") or []:
                    lines.append(f"MESSAGE {m['role']} {m['content']}")
                text = "\n".join(lines)
            modelfile.create(name, text, say)
        except Exception as e:  # noqa: BLE001
            say({"error": str(e)})
        say(None)
    threading.Thread(target=go, daemon=True).start()
    if not b.get("stream", True):
        last: dict = {}
        while (item := await q.get()) is not None:
            last = item
        return JSONResponse(last, status_code=400 if "error" in last else 200)

    async def gen():
        while (item := await q.get()) is not None:
            yield (json.dumps(item) + "\n").encode()
    return _ndjson(gen())


async def copy_ep(request: Request):
    b = await request.json()
    try:
        await asyncio.to_thread(models.copy, b["source"], b["destination"])
        return Response(status_code=200)
    except (LocalAIError, KeyError) as e:
        return _err(str(e), 404)


async def delete_ep(request: Request):
    b = await request.json()
    try:
        await asyncio.to_thread(engine.unload, b.get("model") or b.get("name") or "")
        await asyncio.to_thread(models.delete, b.get("model") or b.get("name") or "")
        return Response(status_code=200)
    except LocalAIError as e:
        return _err(str(e), 404)


async def version(request: Request):
    return JSONResponse({"version": VERSION}, headers={"X-ABP-LocalAI": "1"})


async def root(request: Request):
    return PlainTextResponse("Ollama is running", headers={"X-ABP-LocalAI": "1"})     # the exact text Ollama clients probe for


# ---- OpenAI-compatible ---------------------------------------------------------------------------------------------- #

async def v1_models(request: Request):
    ms = await asyncio.to_thread(models.listing)
    return JSONResponse({"object": "list", "data": [{"id": m["name"], "object": "model", "created": 0, "owned_by": "abp"} for m in ms]})


async def _v1_proxy(request: Request, path: str, embedding: bool = False):
    try:
        b = await request.json()
        block = "" if embedding else await asyncio.to_thread(_memory, request, b, _last_user(b.get("messages") or []))
        model = b.get("model", "")
        rec = await asyncio.to_thread(models.resolve, model)
        r = await _load(model, {}, None, embedding)
        if path == "/v1/chat/completions" and rec.get("system") and not any(m.get("role") == "system" for m in b.get("messages", [])):
            b["messages"] = [{"role": "system", "content": rec["system"]}] + b.get("messages", [])
        if block and isinstance(b.get("messages"), list):        # after the model's own system prompt, into the same message
            b["messages"] = _with_memory(b["messages"], block)
        if b.get("stream"):
            async def gen():
                r.busy += 1
                try:
                    async with _client.stream("POST", f"{r.url}{path}", json=b) as resp:
                        async for chunk in resp.aiter_raw():
                            yield chunk
                finally:
                    r.busy -= 1
                    r.last_used = time.time()
            return StreamingResponse(gen(), media_type="text/event-stream")
        r.busy += 1
        try:
            resp = await _client.post(f"{r.url}{path}", json=b)
        finally:
            r.busy -= 1
            r.last_used = time.time()
        j = resp.json()
        if isinstance(j, dict):
            j["model"] = rec["name"]
        return JSONResponse(j, status_code=resp.status_code)
    except LocalAIError as e:
        return JSONResponse({"error": {"message": str(e), "type": "invalid_request_error"}}, 404 if "not found" in str(e) else 400)


async def v1_chat(request: Request):
    return await _v1_proxy(request, "/v1/chat/completions")


async def v1_completions(request: Request):
    return await _v1_proxy(request, "/v1/completions")


async def v1_embeddings(request: Request):
    return await _v1_proxy(request, "/v1/embeddings", embedding=True)


def build_app() -> Starlette:
    routes = [
        Route("/", root, methods=["GET", "HEAD"]), Route("/api/version", version),
        Route("/api/generate", generate, methods=["POST"]), Route("/api/chat", chat, methods=["POST"]),
        Route("/api/embed", embed, methods=["POST"]), Route("/api/embeddings", embeddings_legacy, methods=["POST"]),
        Route("/api/tags", tags), Route("/api/ps", ps), Route("/api/show", show, methods=["POST"]),
        Route("/api/pull", pull_ep, methods=["POST"]), Route("/api/push", push, methods=["POST"]),
        Route("/api/create", create, methods=["POST"]), Route("/api/blobs/{digest}", blobs, methods=["HEAD", "POST"]),
        Route("/api/copy", copy_ep, methods=["POST"]), Route("/api/delete", delete_ep, methods=["DELETE"]),
        Route("/v1/models", v1_models), Route("/v1/chat/completions", v1_chat, methods=["POST"]),
        Route("/v1/completions", v1_completions, methods=["POST"]), Route("/v1/embeddings", v1_embeddings, methods=["POST"]),
    ]
    async def bad_json(request: Request, exc: Exception) -> JSONResponse:
        return _err(f"the request body is not valid JSON: {exc}", 400)

    async def bad_request(request: Request, exc: Exception) -> JSONResponse:
        return _err(str(exc), 400)

    # what Ollama answers for a malformed request: {"error": ...} with 400, never a server error
    app = Starlette(routes=routes, exception_handlers={json.JSONDecodeError: bad_json, LocalAIError: bad_request})

    def reaper():
        while True:
            time.sleep(5)
            try:
                engine.reap()
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=reaper, daemon=True, name="localai-reaper").start()
    return app


def main(argv=None) -> int:
    import atexit

    import uvicorn
    ap = argparse.ArgumentParser(prog="python -m bot.localai.server", description="ABP's Ollama-compatible model server")
    st = engine.settings()
    ap.add_argument("--port", type=int, default=int(st.get("port", 11436)))
    ap.add_argument("--bind", default=st.get("bind", "127.0.0.1"))
    a = ap.parse_args(argv)
    atexit.register(engine.unload)
    uvicorn.run(build_app(), host=a.bind, port=a.port, log_level="warning", server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

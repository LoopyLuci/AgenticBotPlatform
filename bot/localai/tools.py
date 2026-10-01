"""The agents' local AI and Neural Lab tools (bot/localai, bot/neurallab). Reads run freely; anything that downloads,
trains, or changes the model store asks the person.

    localai_status    the model server, engines, GPUs, models (pulled, imported, referenced), what is loaded, runs
    localai_discover  models other programs keep on this machine (Ollama, LM Studio, Hugging Face, Kestrion...)
    localai_models    pull / import / reference / copy / delete / create (Modelfile) a model (asks first)
    localai_train     fine-tune a model on the GPU (LoRA SFT or DPO), stop a run, export it (asks first); runs and
                      their progress are in localai_status
    lab_status        the Neural Lab: runs, designs, the toolkit's projects, the system models, the CPU policy
    lab_design        check a design (a spec): shapes, parameters, active parameters, FLOPs
    lab_train         train a design on the GPU (asks first)
    lab_import        bring a BrainBuilder graph or a KotMoE design / checkpoint in as a design
    lab_systune       the system models: advice (copy settings, model options, memory forecast, CPU policy, status)
                      runs freely; measuring the drives or the GPU and retraining ask first (per-call approval)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

MAX_OUT = 14_000


def _out(value: Any) -> str:
    text = json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


async def _call(fn, *args, **kwargs) -> str:
    try:
        return _out(await asyncio.to_thread(fn, *args, **kwargs))
    except Exception as e:  # noqa: BLE001 - reaches the agent as text
        return f"Error: {e}" if type(e).__name__ in ("LocalAIError", "SpecError") else f"Error: {type(e).__name__}: {e}"


def _models(inp: dict) -> Any:
    from bot.localai import models, modelfile, pull
    a = inp.get("action")
    if a == "pull":
        return pull.pull(inp["name"])
    if a == "import":
        return models.import_file(inp["name"], inp["path"])
    if a == "reference":
        return models.add_reference(inp["name"], inp["path"])
    if a == "copy":
        models.copy(inp["name"], inp["destination"])
        return {"copied": True}
    if a == "delete":
        return {"deleted": models.delete(inp["name"])}
    if a == "create":
        return modelfile.create(inp["name"], inp["modelfile"])
    raise ValueError("action is pull, import, reference, copy, delete or create")


def _train(inp: dict) -> Any:
    from bot.localai import train
    a = inp.get("action", "start")
    if a == "start":
        hp = {k: inp[k] for k in ("method", "rank", "alpha", "learning_rate", "epochs", "max_steps", "batch_size",
                                  "grad_accum", "max_seq_length") if k in inp}
        return train.start(inp["base"], inp["data"], inp.get("name", ""), inp.get("export", "q4_k_m"), **hp)
    if a == "stop":
        return train.stop(inp["run"])
    if a == "export":
        return train.export(inp["run"], inp.get("quant", ""), inp.get("name", ""))
    raise ValueError("action is start, stop or export")


def _lab_import(inp: dict) -> Any:
    from bot.neurallab import interop, lab, spec
    k = inp["kind"]
    if k == "brainbuilder":
        s = interop.from_bbir(inp["path"])
    elif k == "kotmoe-registry":
        row = next(r for r in interop.kotmoe_registry() if r["id"] == inp["id"])
        s = interop.from_kotmoe_registry(row)
    elif k == "kotmoe-checkpoint":
        s = interop.from_kmoe(inp["path"])
    else:
        raise ValueError("kind is brainbuilder, kotmoe-registry or kotmoe-checkpoint")
    saved = lab.save_design(s, inp.get("name", ""))
    return {"design": saved["name"], "text": spec.describe(s)}


def _systune(inp: dict) -> Any:
    from bot.neurallab import systune
    a = inp.get("action", "status")
    if a == "status":
        return systune.status()
    if a == "advise_copy":
        return systune.advise_copy(inp["src"], inp["dst"], int(float(inp.get("size_gb", 4)) * 2**30), int(inp.get("files", 100)))
    if a == "advise_llm":
        return systune.advise_llm(inp["model"])
    if a == "forecast_memory":
        return systune.forecast_memory()
    if a == "cpu_policy":
        return systune.cpu_policy()
    if a == "bench_drives":
        return systune.bench_all()
    if a == "bench_llm":
        return systune.bench_llm(inp["model"])
    if a == "train":
        return systune.train_model(inp["kind"])
    raise ValueError("action is status, advise_copy, advise_llm, forecast_memory, cpu_policy, bench_drives, bench_llm or train")


SYSTUNE_FREE = frozenset({"status", "advise_copy", "advise_llm", "forecast_memory", "cpu_policy"})


def systune_needs_approval(inp: dict) -> bool:
    """Asks unless the action is one of the read-only ones (an allow-list: anything else, or none, asks)."""
    return inp.get("action") not in SYSTUNE_FREE


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    def reg(name, description, props, required, handler, *, read_only=True, needs_approval=None):
        toolspec.register(
            {"name": name, "description": description,
             "input_schema": {"type": "object", "properties": props, "required": required}},
            toolspec.ToolSpec(name, "read" if read_only else "external", read_only=read_only,
                              concurrency_safe=read_only, origin="registered", needs_approval=needs_approval),
            handler)

    async def status(inp, **_):
        from bot.localai import service
        return await _call(service.overview)

    async def disc(inp, **_):
        from bot.localai import discover
        return await _call(discover.scan)

    async def models_t(inp, **_):
        return await _call(_models, inp)

    async def train_t(inp, **_):
        return await _call(_train, inp)

    async def lab_status(inp, **_):
        from bot.neurallab import service
        return await _call(service.overview)

    async def lab_design(inp, **_):
        from bot.neurallab import spec
        return await _call(lambda s: {"text": spec.describe(s), "stats": spec.validate(s)["stats"]}, inp["spec"])

    async def lab_train(inp, **_):
        from bot.neurallab import lab
        return await _call(lab.start, inp["spec"], inp["data"], inp.get("train"), inp.get("label", ""))

    async def lab_imp(inp, **_):
        return await _call(_lab_import, inp)

    async def tune(inp, **_):
        return await _call(_systune, inp)

    reg("localai_status", "ABP's local AI: the Ollama-compatible model server (port 11436), llama.cpp engines, GPUs, every "
        "model (pulled, imported, referenced from other programs), what is loaded, fine-tuning runs.", {}, [], status)
    reg("localai_discover", "Models other programs already keep on this machine (Ollama, LM Studio, the Hugging Face cache, "
        "GPT4All, Jan, Kestrion, added folders), and which ABP already uses.", {}, [], disc)
    reg("localai_models", "Change ABP's model store: pull a model (\"qwen2.5:0.5b\", \"hf.co/<repo>:<quant>\"), import or "
        "reference a GGUF file, copy, delete, or create one from a Modelfile. Asks first.",
        {"action": {"type": "string", "enum": ["pull", "import", "reference", "copy", "delete", "create"]},
         "name": {"type": "string"}, "path": {"type": "string"}, "destination": {"type": "string"},
         "modelfile": {"type": "string"}}, ["action", "name"], models_t, read_only=False, needs_approval=True)
    reg("localai_train", "Fine-tune a language model on the GPU (LoRA; method sft or dpo) from a Hugging Face checkpoint and "
        "JSONL data, then it is merged, converted to GGUF, quantized and added to the store. Also stop/export/status of "
        "a run. Asks first.",
        {"action": {"type": "string", "enum": ["start", "stop", "export"]}, "base": {"type": "string"},
         "data": {"type": "array", "items": {"type": "string"}}, "name": {"type": "string"}, "run": {"type": "string"},
         "method": {"type": "string", "enum": ["sft", "dpo"]}, "rank": {"type": "integer"}, "learning_rate": {"type": "number"},
         "epochs": {"type": "number"}, "max_steps": {"type": "integer"}, "export": {"type": "string"}, "quant": {"type": "string"}},
        ["action"], train_t, read_only=False, needs_approval=True)
    reg("lab_status", "The Neural Lab: recent training runs, saved designs, the toolkit's projects (BrainBuilder, KotMoE, "
        "Amethyst, Kestrion), the ops a design can use, and the system models with the current CPU policy.", {}, [], lab_status)
    reg("lab_design", "Check a model design (a Neural Lab spec): every node's output shape, parameters, active parameters "
        "(mixture-of-experts layers run top_k experts), FLOPs per row and training memory.",
        {"spec": {"type": "object"}}, ["spec"], lab_design)
    reg("lab_train", "Train a Neural Lab design on the GPU. data: {path (csv/jsonl/npz/text), target?, features?, "
        "tokenizer? (byte, words, a KotMoE .bpe, an HF tokenizer folder)}. Asks first.",
        {"spec": {"type": "object"}, "data": {"type": "object"}, "train": {"type": "object"}, "label": {"type": "string"}},
        ["spec", "data"], lab_train, read_only=False, needs_approval=True)
    reg("lab_import", "Bring a design in from the toolkit's projects: a BrainBuilder graph (*.bbir.edn), a KotMoE registry "
        "design (id), or a kotmoe-gen checkpoint (model.kmoe); saved as a lab design.",
        {"kind": {"type": "string", "enum": ["brainbuilder", "kotmoe-registry", "kotmoe-checkpoint"]},
         "path": {"type": "string"}, "id": {"type": "string"}, "name": {"type": "string"}}, ["kind"], lab_imp)
    reg("lab_systune", "The system models that tune this machine. Free: copy settings for two drives (advise_copy src "
        "dst size_gb files), llama.cpp options for a model (advise_llm), RAM use in five minutes (forecast_memory), which "
        "processors ABP's heavy work may use and why (cpu_policy), every model's state (status). Asks first: "
        "bench_drives (unbuffered I/O on every drive with room, about 2.5 GB each, removed after), bench_llm "
        "(llama-bench on the GPU for a model), train (kind: transfer, llm, memory, stability).",
        {"action": {"type": "string", "enum": ["status", "advise_copy", "advise_llm", "forecast_memory", "cpu_policy",
                                               "bench_drives", "bench_llm", "train"]},
         "src": {"type": "string"}, "dst": {"type": "string"}, "size_gb": {"type": "number"}, "files": {"type": "integer"},
         "model": {"type": "string"}, "kind": {"type": "string", "enum": ["transfer", "llm", "memory", "stability"]}},
        ["action"], tune, read_only=False, needs_approval=systune_needs_approval)

register_tools()

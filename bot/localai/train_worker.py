"""The fine-tuning worker: runs inside the training environment (<home>/venv-train, PyTorch on the GPU), never in ABP's
own process. ABP's bot/localai/train.py writes a job.json and starts this file; it reports progress as JSON lines on
stdout and in <run>/status.json.

    python train_worker.py <run dir>

What it does (what Unsloth does, on PyTorch + PEFT, AMD ROCm or NVIDIA CUDA):
    sft    LoRA (or rsLoRA / DoRA) supervised fine-tuning on chat data, loss on the assistant's turns only
    dpo    Direct Preference Optimization on (prompt, chosen, rejected); the reference model is the base with the
           adapter switched off (no second copy in memory)
    then   save the adapter; optionally merge it into the base (16-bit safetensors), ready for GGUF conversion

Kept to a plain training loop on purpose (AdamW, warmup + cosine, gradient accumulation, bf16 autocast, gradient
checkpointing, length-grouped batches) so it does not depend on fast-moving trainer APIs. CPU threads are capped: the
host machine must not run sustained all-core loads.

Standalone: imports nothing from ABP (the training environment does not have ABP's dependencies).
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import time
from pathlib import Path

RUN = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
STATUS = RUN / "status.json"
STOP = RUN / "stop"


def report(**kv) -> None:
    kv["time"] = time.time()
    try:
        cur = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cur = {}
    cur.update(kv)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur), encoding="utf-8")
    os.replace(tmp, STATUS)
    print(json.dumps(kv), flush=True)


# ---- datasets ----------------------------------------------------------------------------------------------------- #

_ROLE = {"human": "user", "user": "user", "gpt": "assistant", "assistant": "assistant", "model": "assistant", "bot": "assistant",
         "system": "system", "tool": "tool"}


def to_messages(row: dict):
    """One training row in any common format -> chat messages (or None). Formats: OpenAI/ChatML "messages",
    ShareGPT "conversations", Alpaca instruction/input/output, prompt/completion, question/answer, ABP Studio's
    instruction/files/diff, plain "text"."""
    if isinstance(row.get("messages"), list):
        return [{"role": _ROLE.get(m.get("role", ""), m.get("role")), "content": m.get("content") or ""} for m in row["messages"]]
    if isinstance(row.get("conversations"), list):
        return [{"role": _ROLE.get(m.get("from") or m.get("role"), "user"), "content": m.get("value") or m.get("content") or ""}
                for m in row["conversations"]]
    if "instruction" in row and ("output" in row or "response" in row):
        user = row["instruction"] + (("\n\n" + row["input"]) if row.get("input") else "")
        msgs = [{"role": "system", "content": row["system"]}] if row.get("system") else []
        return msgs + [{"role": "user", "content": user}, {"role": "assistant", "content": row.get("output") or row.get("response")}]
    if "instruction" in row and "diff" in row:                       # ABP Studio: a change people kept
        files = ", ".join(row.get("files") or [])
        user = row["instruction"] + (f"\n\nFiles: {files}" if files else "")
        return [{"role": "user", "content": user}, {"role": "assistant", "content": row["diff"]}]
    for q, a in (("prompt", "completion"), ("question", "answer"), ("input", "output")):
        if q in row and a in row:
            return [{"role": "user", "content": str(row[q])}, {"role": "assistant", "content": str(row[a])}]
    return None


def to_pref(row: dict):
    """-> (prompt messages, chosen text, rejected text) or None."""
    if "chosen" not in row or "rejected" not in row:
        return None
    def text(v):
        if isinstance(v, list):                       # chosen/rejected given as full conversations: the last assistant turn
            return next((m.get("content", "") for m in reversed(v) if m.get("role") == "assistant"), "")
        if isinstance(v, dict):
            return v.get("diff") or v.get("content") or json.dumps(v)
        return str(v)
    prompt = row.get("prompt") or row.get("instruction") or row.get("question")
    if isinstance(prompt, list):
        pm = prompt
    elif prompt:
        pm = [{"role": "user", "content": str(prompt)}]
    elif isinstance(row["chosen"], list):
        last = max(i for i, m in enumerate(row["chosen"]) if m.get("role") == "assistant")
        pm = row["chosen"][:last]
    else:
        return None
    return pm, text(row["chosen"]), text(row["rejected"])


def load_rows(paths: list[str]) -> list[dict]:
    rows = []
    for p in paths:
        p = Path(p)
        if p.suffix.lower() == ".json":
            data = json.loads(p.read_text(encoding="utf-8"))
            rows += data if isinstance(data, list) else data.get("data", [])
        elif p.suffix.lower() == ".parquet":
            import datasets
            rows += list(datasets.load_dataset("parquet", data_files=str(p), split="train"))
        else:
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
    return rows


# ---- tokenizing --------------------------------------------------------------------------------------------------- #

def encode_sft(tok, msgs, max_len: int):
    """input_ids and labels with -100 everywhere except the assistant's turns (train on responses only)."""
    if not getattr(tok, "chat_template", None):
        text = "".join(f"<|{m['role']}|>\n{m['content']}\n" for m in msgs) + (tok.eos_token or "")
        ids = tok(text, add_special_tokens=True)["input_ids"][:max_len]
        return ids, list(ids)
    # Render the text, find each assistant turn's character span, then label the tokens inside those spans. (Working
    # in text, not token lists, survives templates that rewrite history, e.g. Qwen3 dropping old <think> blocks.)
    full = tok.apply_chat_template(msgs, tokenize=False)
    spans, pos = [], 0
    for i, m in enumerate(msgs):
        if m["role"] != "assistant":
            continue
        pre = tok.apply_chat_template(msgs[:i], tokenize=False, add_generation_prompt=True) if i else ""
        upto = tok.apply_chat_template(msgs[: i + 1], tokenize=False)
        if i and full.startswith(upto) and upto.startswith(pre):
            spans.append((len(pre), len(upto)))
        else:
            at = full.find(m["content"], pos)
            if at < 0:
                continue
            end = at + len(m["content"])
            eot = full.find("<|", end)                     # include the end-of-turn token the model must learn to emit
            spans.append((at, full.find(">", eot) + 1 if 0 <= eot < end + 40 else end))
        pos = spans[-1][1]
    enc = tok(full, add_special_tokens=False, return_offsets_mapping=True)
    ids, labels = list(enc["input_ids"]), []
    for t, (a, b) in zip(ids, enc["offset_mapping"]):
        labels.append(t if any(s <= a and b <= e for s, e in spans) and b > a else -100)
    return ids[:max_len], labels[:max_len]


def encode_pref(tok, prompt_msgs, answer: str, max_len: int):
    text = tok.apply_chat_template(prompt_msgs, tokenize=False, add_generation_prompt=True) if tok.chat_template else \
        "".join(m["content"] for m in prompt_msgs)
    p = tok(text, add_special_tokens=False)["input_ids"]
    a = tok(answer + (tok.eos_token or ""), add_special_tokens=False)["input_ids"]
    ids = (p + a)[:max_len]
    labels = ([-100] * len(p) + a)[:max_len]
    return ids, labels


def batches(items, bs: int, pad: int, shuffle: bool, seed: int):
    """Length-grouped batches (similar lengths together: less padding), in random order."""
    import torch
    idx = list(range(len(items)))
    rnd = random.Random(seed)
    if shuffle:
        rnd.shuffle(idx)
    groups = [sorted(idx[i:i + bs * 50], key=lambda j: len(items[j][0])) for i in range(0, len(idx), bs * 50)]
    out = [g[i:i + bs] for g in groups for i in range(0, len(g), bs)]
    if shuffle:
        rnd.shuffle(out)
    for b in out:
        n = max(len(items[j][0]) for j in b)
        ids = torch.full((len(b), n), pad, dtype=torch.long)
        lab = torch.full((len(b), n), -100, dtype=torch.long)
        att = torch.zeros((len(b), n), dtype=torch.long)
        for r, j in enumerate(b):
            x, y = items[j][0], items[j][1]
            ids[r, : len(x)] = torch.tensor(x)
            lab[r, : len(y)] = torch.tensor(y)
            att[r, : len(x)] = 1
        yield ids, lab, att


def seq_logp(model, ids, lab, att):
    """Sum of log-probabilities of the labelled tokens, per row."""
    import torch
    logits = model(input_ids=ids, attention_mask=att).logits[:, :-1].float()
    tgt = lab[:, 1:]
    mask = tgt != -100
    lp = torch.log_softmax(logits, -1).gather(-1, tgt.clamp(min=0).unsqueeze(-1)).squeeze(-1)
    return (lp * mask).sum(-1)


# ---- the run ------------------------------------------------------------------------------------------------------ #

def main() -> int:
    job = json.loads((RUN / "job.json").read_text(encoding="utf-8"))
    import torch
    torch.set_num_threads(int(job.get("cpu_threads", 4)))
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import LoraConfig, PeftModel, get_peft_model

    seed = int(job.get("seed", 3407))
    random.seed(seed)
    torch.manual_seed(seed)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    if dev == "cpu" and not job.get("allow_cpu"):
        report(state="failed", error="no GPU visible to PyTorch (training on this machine's CPU is not allowed)")
        return 2
    dtype = torch.bfloat16 if dev == "cuda" and torch.cuda.is_bf16_supported() else torch.float16 if dev == "cuda" else torch.float32
    report(state="loading", device=torch.cuda.get_device_name(0) if dev == "cuda" else "cpu", dtype=str(dtype).split(".")[-1])

    tok = AutoTokenizer.from_pretrained(job["base"])
    if len(tok) < 256:
        report(state="failed", error=f"{job['base']} has no usable tokenizer (tokenizer.json / vocab files are missing)")
        return 2
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(job["base"], dtype=dtype, attn_implementation="sdpa").to(dev)
    if job.get("gradient_checkpointing", True):
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        model.enable_input_require_grads()
    model.config.use_cache = False
    targets = job.get("target_modules") or ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
    present = {n.rsplit(".", 1)[-1] for n, _ in model.named_modules()}
    targets = [t for t in targets if t in present] or "all-linear"
    if job.get("resume_adapter"):
        model = PeftModel.from_pretrained(model, job["resume_adapter"], is_trainable=True)
    else:
        model = get_peft_model(model, LoraConfig(r=int(job.get("rank", 16)), lora_alpha=int(job.get("alpha", 16)),
                                                 lora_dropout=float(job.get("dropout", 0.0)), target_modules=targets,
                                                 use_rslora=bool(job.get("rslora")), use_dora=bool(job.get("dora")),
                                                 task_type="CAUSAL_LM"))
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())

    method = job.get("method", "sft")
    max_len = int(job.get("max_seq_length", 2048))
    rows = load_rows(job["data"])
    items = []
    for r in rows:
        if method == "dpo":
            pr = to_pref(r)
            if pr:
                c = encode_pref(tok, pr[0], pr[1], max_len)
                j = encode_pref(tok, pr[0], pr[2], max_len)
                items.append((c[0], c[1], j[0], j[1]))
        else:
            m = to_messages(r)
            if m and any(x["role"] == "assistant" for x in m):
                ids, lab = encode_sft(tok, m, max_len)
                if any(l != -100 for l in lab):
                    items.append((ids, lab))
    if not items:
        report(state="failed", error=f"none of the {len(rows)} rows is usable for {method} (see docs/localai.md for the formats)")
        return 2
    rnd = random.Random(seed)
    rnd.shuffle(items)
    n_eval = min(int(len(items) * float(job.get("eval_fraction", 0.0))), 200)
    evals, items = items[:n_eval], items[n_eval:]

    bs, accum, epochs = int(job.get("batch_size", 2)), int(job.get("grad_accum", 4)), float(job.get("epochs", 1))
    steps_per_epoch = math.ceil(len(items) / bs / accum)
    total_steps = int(job.get("max_steps") or max(1, math.ceil(steps_per_epoch * epochs)))
    warm = int(job.get("warmup_steps", max(1, total_steps // 20)))
    lr = float(job.get("learning_rate", 2e-4 if method == "sft" else 5e-6))
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=lr, weight_decay=float(job.get("weight_decay", 0.0)),
                            betas=(0.9, 0.999))
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: (s + 1) / warm if s < warm else
                                              0.5 * (1 + math.cos(math.pi * (s - warm) / max(1, total_steps - warm))))
    beta = float(job.get("dpo_beta", 0.1))
    report(state="training", rows=len(rows), examples=len(items), eval_examples=len(evals), total_steps=total_steps,
           trainable_params=trainable, total_params=total, method=method)

    def loss_for(batch):
        if method == "dpo":
            ci, cl, ca, ri, rl, ra = batch
            with torch.autocast(dev, dtype=dtype, enabled=dev == "cuda"):
                pc, pr_ = seq_logp(model, ci, cl, ca), seq_logp(model, ri, rl, ra)
                with torch.no_grad(), model.disable_adapter():
                    rc, rr = seq_logp(model, ci, cl, ca), seq_logp(model, ri, rl, ra)
            margin = beta * ((pc - rc) - (pr_ - rr))
            return -torch.nn.functional.logsigmoid(margin).mean(), (margin > 0).float().mean().item()
        ids, lab, att = batch
        with torch.autocast(dev, dtype=dtype, enabled=dev == "cuda"):
            return model(input_ids=ids, attention_mask=att, labels=lab).loss, None

    def stream(data, shuffle, s):
        if method == "dpo":
            ch = [(x[0], x[1]) for x in data]
            rj = [(x[2], x[3]) for x in data]
            for a, b in zip(batches(ch, bs, tok.pad_token_id, False, s), batches(rj, bs, tok.pad_token_id, False, s)):
                yield tuple(t.to(dev) for t in a + b)
        else:
            for b in batches(data, bs, tok.pad_token_id, shuffle, s):
                yield tuple(t.to(dev) for t in b)

    def evaluate():
        if not evals:
            return None
        model.eval()
        tot, n = 0.0, 0
        with torch.no_grad():
            for b in stream(evals, False, 0):
                l, _ = loss_for(b)
                tot, n = tot + l.item(), n + 1
        model.train()
        return tot / max(1, n)

    model.train()
    step, micro, t0, hist = 0, 0, time.time(), []
    out = RUN / "adapter"
    epoch = 0
    while step < total_steps:
        for batch in stream(items, True, seed + epoch):
            loss, acc = loss_for(batch)
            (loss / accum).backward()
            micro += 1
            hist.append(loss.item())
            if micro % accum:
                continue
            torch.nn.utils.clip_grad_norm_(model.parameters(), float(job.get("max_grad_norm", 1.0)))
            opt.step()
            sched.step()
            opt.zero_grad(set_to_none=True)
            step += 1
            el = time.time() - t0
            info = {"step": step, "loss": round(sum(hist) / len(hist), 4), "lr": sched.get_last_lr()[0], "epoch": epoch,
                    "elapsed": round(el, 1), "eta": round(el / step * (total_steps - step), 1)}
            if acc is not None:
                info["reward_accuracy"] = round(acc, 3)
            if dev == "cuda":
                info["gpu_mem_gb"] = round(torch.cuda.max_memory_allocated() / 2**30, 2)
            hist = []
            if job.get("eval_every") and step % int(job["eval_every"]) == 0:
                info["eval_loss"] = evaluate()
            if job.get("save_every") and step % int(job["save_every"]) == 0:
                model.save_pretrained(RUN / f"checkpoint-{step}")
            report(state="training", **info)
            if STOP.exists():
                report(state="stopping", note="stop requested: saving what was learned so far")
                step = total_steps
            if step >= total_steps:
                break
        epoch += 1
    final_eval = evaluate()
    model.save_pretrained(out)
    tok.save_pretrained(out)
    report(state="saved", adapter=str(out), eval_loss=final_eval)

    if job.get("merge", True):
        report(state="merging")
        merged = model.merge_and_unload()
        merged.config.use_cache = True
        mdir = RUN / "merged"
        merged.save_pretrained(mdir, safe_serialization=True, max_shard_size="4GB")
        tok.save_pretrained(mdir)
        report(state="merged", merged=str(mdir))
    report(state="done", steps=step)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                                       # noqa: BLE001 - the run's status says what broke
        import traceback
        report(state="failed", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-4000:])
        sys.exit(1)

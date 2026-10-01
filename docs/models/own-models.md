# ABP's own models (roadmap §5.13: AM-A findings, AM-B designs)

Small, specific models for decisions ABP makes many times a day. Each one has a heuristic it replaces (and falls back
to), learns from data ABP already logs, is judged offline, then runs in shadow next to the heuristic, and is promoted
only when it is better on the same requests.

## AM-A: what the engines can do today (checked 2026-09-30)

| Engine | What is real | What is not |
|---|---|---|
| **KotMoE** (Kotlin core, `src/main/kotlin/com/kotmoe`) | A pure-Kotlin tensor library with autograd, a mixture-of-experts with a learned router, real training: 64 experts on MNIST reach 95.7% test accuracy (`run_8expert.log`), CPU only. | No tokenizer or sequence model, so no text generation. The desktop app's chat engine (`tauri-app/src-tauri/src/kotmoe_engine.rs`, `run_model_inference`) does not run a model: it answers with text chosen by keywords in the prompt. Its `model/onnx.rs` loader is not wired into that path. No ONNX export. |
| **BrainBuilder** (Rust orchestrator + a PyTorch backend) | Real PyTorch training with live loss, checkpoints, prediction; components include linear, attention, convolution, embeddings, LoRA; templates for an MLP classifier, a transformer stack, a text sentiment classifier, a next-word predictor and a speculative-decoding drafter; models from a goal or a description; new components synthesized and smoke-tested. | Not built to train or serve a code-writing language model. Serving from ABP needs an export: PyTorch's ONNX export, then ABP runs it with OpenCV DNN (the runtime bot/vision already uses). |
| **ABP itself** | `bot/unsloth`: LoRA fine-tuning of open language models; `bot/vision`: an ONNX runtime (OpenCV DNN) with a per-model engine choice; Studio's datasets (`data/studio/datasets`). | No shared train-evaluate-promote loop yet (AM-C). |

So: **KotMoE** trains the small, fast classifiers (CPU, microseconds per decision); **BrainBuilder** trains the sequence
models, detectors and rankers (PyTorch, GPU); the one generative model (GEN-1's edit writer) is a LoRA fine-tune of a
small open code model through ABP's Unsloth integration, orchestrated from BrainBuilder. Before KotMoE serves anything
in ABP it needs either an ONNX export or a small inference server; its desktop chat should run a real model (or say it
has none) rather than keyword-picked text.

## AM-B: the models

Each: what it decides, what it sees, where its labels come from, the engine, its budget, how it is judged.

| Model | Decides | Inputs | Labels (already logged) | Engine | Budget | Judged by |
|---|---|---|---|---|---|---|
| **route** | which backend and model answers a request | prompt length, language, code/math/tool cues, attachments, the chat's recent models; each model's recent success, latency, cost | turn outcome: finished without retry, the person's rating, cost and time (router and usage logs) | KotMoE MoE classifier (experts per kind of request) | < 5 ms CPU | success rate at equal or lower cost than the router's rules, on replayed requests |
| **intent** | which slash command or tool family a message wants | the message (hashed n-grams or a small embedding) | slash commands people typed after a plain message; tools the agent ended up calling | BrainBuilder text classifier (its sentiment template, retargeted) | < 20 ms CPU | top-1 / top-3 accuracy on held-out conversations |
| **tool-risk** | whether a tool call is likely to fail (ask first, or pick another tool) | tool name, argument shapes, workspace, recent failures | tool results (error / ok) in the agent traces | KotMoE classifier | < 2 ms | precision at the recall that would have caught half the failures |
| **sentinel** | whether the server is heading for trouble (a crash, a stall, a full disk) | metrics and log-rate series (Sentinel's journal) | incidents Sentinel recorded, restarts, bug-hunter issues | BrainBuilder 1-D convolution / attention over windows | runs each minute | warning lead time and false alarms per day |
| **prefetch** | which cache keys CacheIt should warm next | the recent access sequence | the accesses that followed | BrainBuilder next-token model (its next-word template over key ids) | < 1 ms per step | hit-rate gain over LRU in a replay |
| **stack** | what a repo is and which detected operations matter (the Module Hub) | file tree, manifests, README | the 48 adopted and catalogued modules, their curated operation lists, operations people call | KotMoE multi-label classifier | < 50 ms | stack F1; how many of the operations people keep it ranks first |
| **screen** | where the buttons, fields, links and text are on a screenshot (CV-D) | a screenshot | free and exact: Studio's previews in a real browser give every element's box from the DOM | BrainBuilder detector (YOLOX-style), ONNX into bot/vision | < 100 ms CPU | mAP on held-out screenshots of pages it did not train on |
| **rank** (GEN-1) | which of a request's variants to show first | the request, each variant's diff, its checks | Studio's prefs.jsonl: chosen against rejected | BrainBuilder small transformer (pairwise) | < 100 ms | how often the variant people keep is ranked first, against chance |
| **edit** (GEN-1, later) | writes Studio's edit blocks itself | the request and the files | Studio's sft.jsonl: changes people kept | LoRA fine-tune of a small open code model (bot/unsloth), from BrainBuilder | seconds | share of its proposals that apply, validate and are kept, against the free models it replaces |

## AM-C: one loop for all of them (next)

1. **Export** a model's dataset from ABP's logs (versioned, with the time window and counts).
2. **Train** with its engine (KotMoE's CLI or BrainBuilder headless), recording the config and data version.
3. **Export** to ONNX; **evaluate** offline on held-out data against the heuristic (the gate).
4. **Shadow**: ABP runs it beside the heuristic and logs both answers, using the heuristic's.
5. **Promote** when it is better on live traffic; **roll back** on a single click or automatically when its live metric
   drops below the heuristic's.

The first to go through it: **stack** (the labels exist now: the curated catalog and the adopted projects) and **screen**
(its labels are generated for free by Studio's browser previews).

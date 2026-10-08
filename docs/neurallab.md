# The Neural Lab

`bot/neurallab` is ABP's toolkit for designing, training and shipping neural networks. It brings ABP's model projects
together around one design format, and it adds system models that learn this machine and tune ABP's own work.

## One design format, every project

A spec (`spec.py`) is a graph of ops. The ops cover:

* **Layers:** linear, LoRA linear, MLP, mixture of experts (top-k routing, balance loss, optional shared expert),
  attention, transformer blocks (dense or MoE), embeddings, GRU, conv1d, layer and RMS norm, pooling, flatten and
  dropout.
* **Heads:** LM heads, plus BrainBuilder's DSpark heads.
* **Combinators and activations:** add, concat, and the activations.

Shapes are inferred node by node. Parameters, active parameters (MoE layers run only top-k experts), FLOPs and training
memory are counted in plain Python. The count is exact: a reference MoE implementation reports the same 1,202,432 parameters
and 938,752 active per token for the same design (tested).

| Project | In ABP |
|---|---|
| **BrainBuilder** | `*.bbir.edn` graphs become specs and go back (`edn.py`, `interop.from_bbir`/`to_bbir`). Text-sequence graphs train as next-word classifiers with a word tokenizer, as BrainBuilder does. |
| **Extensions** | Importers, projects, model families and worker plugins (checkpoint formats, tokenizers) a private extension adds: `api.add_lab_importer`, `add_lab_project`, `add_lab_family`, `add_lab_worker_plugin` (docs/extensions.md). |
| **Amethyst** | A fine-tuned LoRA becomes an Amethyst module (`.apkg`): manifest hashed as Amethyst hashes it, PEFT weights, and a benchmark from the training data. It is installed with Amethyst's own CLI, which lives in the training environment beside the GPU build of PyTorch, so Amethyst's modules load on the GPU. |

Training (`nn_worker.py`) runs in the training environment, on the GPU. It supports classification, regression,
language models, next-token prediction and anomaly autoencoders. It uses AdamW with warmup and cosine decay, bf16
autocast for token models, GPT-2 initialisation for token models, early stopping on validation loss with the
best checkpoint kept, expert-load reporting, and exports to safetensors, npz and model.json (plus every checkpoint format a worker plugin writes for
the design; `init_from {format, path}` starts from one). `lab.autotune` searches design settings one GPU run at a time.

### Verified (2026-10-01)

* **BrainBuilder's story graph:** 87% next-word validation accuracy; it round-trips back to BrainBuilder's format.

## System models: tuning this machine

`telemetry.py` measures and `systune.py` learns. Every model is a small mixture of experts trained on the GPU and run
with numpy inside ABP (`infer.py`). Inputs it never saw are clipped, so they can't saturate it. The models only steer
ABP's own work: block sizes, parallelism, model options, unloading, and the processor affinity of ABP's own child
processes. No system setting is changed.

| Model | Learns from | Decides |
|---|---|---|
| transfer | Unbuffered I/O on every drive (`bench_drive`), and real copies larger than half of free memory | The block size and parallel files for each copy the file server's transfer engine makes. A drive's own measurement wins over the model; parallelism never goes beyond what was measured. |
| llm | `llama-bench` on the GPU (batch, micro-batch, flash attention) | `-b`/`-ub`/`-fa` when ABP's server starts a model (`auto_tune`) |
| memory | Telemetry: RAM use 5 minutes ahead | Idle models unloaded early when the forecast passes 90% |
| stability | Machine-check (WHEA) history by APIC id; unexpected power losses; an autoencoder of this machine's normal load patterns | Which processors ABP's heavy processes may use, and how many threads |

What the measurements found on one machine is kept with that machine's private notes.

## Interfaces

* **Pages:** Local AI and Neural Lab in the dashboard and the desktop app (`ai-panel.js`, identical in both).
* **TUI:** key `l`.
* **CLI:** `abp ai …` and `abp lab …` (an extension may add its own commands).
* **API:** `/api/localai/*` and `/api/lab/*`.
* **MCP:** `localai_status`, `localai_discover`, `lab_status`, `lab_validate`, `lab_advice`.
* **Agent tools:** `localai_*` and `lab_*`. `lab_systune` uses per-call approval: advice runs freely; measuring and
  retraining ask.

### Per-call approval

`ToolSpec.needs_approval` may be a function of the call's input. It fails closed: no input, an exception, or a
non-bool answer all mean "ask". The function sees a copy of the input and is judged on the input as it will run,
after hooks. Plan mode and taint treat such a tool as one that changes things. Write it as an allow-list.

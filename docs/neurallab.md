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
memory are counted in plain Python. The count is exact: KotMoE's Kotlin runtime reports the same 1,202,432 parameters
and 938,752 active per token for the same design (tested).

| Project | In ABP |
|---|---|
| **KotMoE** | Registry designs (the MNIST MoE classifiers) become specs. kotmoe-gen checkpoints (`model.kmoe`, big-endian, KotMoE's parameter order) are read and written, so a model trains on the GPU here and goes back to KotMoE. Its MNIST IDX files become a dataset. KotMoE's byte-level BPE is ported (`nn_worker.KotMoETok`). |
| **BrainBuilder** | `*.bbir.edn` graphs become specs and go back (`edn.py`, `interop.from_bbir`/`to_bbir`). Text-sequence graphs train as next-word classifiers with a word tokenizer, as BrainBuilder does. |
| **Amethyst** | A fine-tuned LoRA becomes an Amethyst module (`.apkg`): manifest hashed as Amethyst hashes it, PEFT weights, and a benchmark from the training data. It is installed with Amethyst's own CLI, which lives in the training environment beside the GPU build of PyTorch, so Amethyst's modules load on the GPU. |
| **Kestrion** | ABP starts Kestrion's inference server with `KESTRION_MODEL_DIRS` set to ABP's store (`{abp_models}` in `abp-ops.toml`, from `ABP_MODELS_DIR`). Kestrion's scanner now reads Ollama-layout stores with real weight paths and allows loading from them. Before this, its Ollama scan matched no files (it looked for names ending in `:latest`) and recorded no paths. ABP's discovery finds `~/.kestrion/models`. |

Training (`nn_worker.py`) runs in the training environment, on the GPU. It supports classification, regression,
language models, next-token prediction and anomaly autoencoders. It uses AdamW with warmup and cosine decay, bf16
autocast for token models, GPT-2/KotMoE initialisation for token models, early stopping on validation loss with the
best checkpoint kept, expert-load reporting, and exports to safetensors, npz and model.json (plus `model.kmoe` for
kotmoe-gen designs). `lab.autotune` searches design settings one GPU run at a time.

### Verified (2026-10-01)

* **KotMoE-gen on the GPU:** 2 layers, 4 experts, ctx 128, KotMoE's 4k BPE, trained on ABP's docs (436 KB). 2000 steps
  took 42 s using 0.35 GB of VRAM; validation perplexity was 121. KotMoE's Kotlin `GenMainKt info`/`sample` loaded the
  exported checkpoint and generated from it.
* **KotMoE registry design `model_0005`:** 128 experts, top-2, 17.1 M parameters, 0.5 M active. On KotMoE's MNIST it
  reached 97.5% validation accuracy in 84 s. KotMoE's registry shows 0.0 for it: it never trained on the CPU.
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

### What the measurements found on this machine

* **The failing core:** WHEA logged fatal machine-check errors on APIC 24 and 25 on 2026-09-30, and on APIC 25 in
  February. CPUID run on each processor maps those to Windows processors 20 and 21: one physical core on the second
  CCD. The stability policy keeps llama-server, quantizing, conversion and training workers off them (verified:
  llama-server's affinity excludes 20 and 21). After a machine-check error in the last 7 days it also halves the
  thread cap.
* **PrimoCache:** a block-level cache sits under the file system. Reads of recently written data come from its RAM, at
  5 to 17 GB/s. Writes to D: (the 8 TB hard disk) are deferred by it.
* **Parallel writes:** writes to every SSD run at about 60 MB/s per file but scale nearly 4× with 4 parallel files.
  This is consistent with per-file scanning on close, so the transfer model learns 4 parallel files for SSD
  destinations.
* **Copies that fit in RAM:** an A/B test of the transfer engine copying 1.1 GB (56 files, E: to X:) showed no gain
  from tuning (about 2.1 GB/s either way), because the copy fits in the page cache. Tuning is therefore applied only to
  copies larger than half of free memory. That case has not yet been A/B tested on large copies.
* **Measured model accuracy:** on held-out measurements the transfer model reached R² 0.98 and the llm model R² 0.98.
  The memory and stability models train themselves once telemetry has 600 and 1500 samples (the recorder runs with ABP).

## Interfaces

* **Pages:** Local AI and Neural Lab in the dashboard and the desktop app (`ai-panel.js`, identical in both).
* **TUI:** key `l`.
* **CLI:** `abp ai …` and `abp lab …`.
* **API:** `/api/localai/*` and `/api/lab/*`.
* **MCP:** `localai_status`, `localai_discover`, `lab_status`, `lab_validate`, `lab_advice`.
* **Agent tools:** `localai_*` and `lab_*`. `lab_systune` uses per-call approval: advice runs freely; measuring and
  retraining ask.

### Per-call approval

`ToolSpec.needs_approval` may be a function of the call's input. It fails closed: no input, an exception, or a
non-bool answer all mean "ask". The function sees a copy of the input and is judged on the input as it will run,
after hooks. Plan mode and taint treat such a tool as one that changes things. Write it as an allow-list.

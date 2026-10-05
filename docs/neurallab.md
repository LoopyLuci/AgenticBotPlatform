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
memory are counted in plain Python. The count is exact: PrivateProject's Kotlin runtime reports the same 1,202,432 parameters
and 938,752 active per token for the same design (tested).

| Project | In ABP |
|---|---|
| **PrivateProject** | Registry designs (the MNIST MoE classifiers) become specs. private_project-gen checkpoints (`model.kmoe`, big-endian, PrivateProject's parameter order) are read and written, so a model trains on the GPU here and goes back to PrivateProject. Its MNIST IDX files become a dataset. PrivateProject's byte-level BPE is ported (`nn_worker.PrivateProjectTok`). |
| **BrainBuilder** | `*.bbir.edn` graphs become specs and go back (`edn.py`, `interop.from_bbir`/`to_bbir`). Text-sequence graphs train as next-word classifiers with a word tokenizer, as BrainBuilder does. |
| **Amethyst** | A fine-tuned LoRA becomes an Amethyst module (`.apkg`): manifest hashed as Amethyst hashes it, PEFT weights, and a benchmark from the training data. It is installed with Amethyst's own CLI, which lives in the training environment beside the GPU build of PyTorch, so Amethyst's modules load on the GPU. |
| **PrivateProject** | ABP starts PrivateProject's inference server with `PRIVATE_PROJECT_MODEL_DIRS` set to ABP's store (`{abp_models}` in `abp-ops.toml`, from `ABP_MODELS_DIR`). PrivateProject's scanner now reads Ollama-layout stores with real weight paths and allows loading from them. Before this, its Ollama scan matched no files (it looked for names ending in `:latest`) and recorded no paths. ABP's discovery finds `~/.private_project/models`. |
| **PRIVATE_PROJECT** | Its checkpoints and their configs, and one rollout from one (`private_project.py`). It is a family ABP runs rather than trains, so it needs no spec: PRIVATE_PROJECT keeps no index, a checkpoint is a `.pt` in a folder, and the model's shape is a YAML beside the code. |

Training (`nn_worker.py`) runs in the training environment, on the GPU. It supports classification, regression,
language models, next-token prediction and anomaly autoencoders. It uses AdamW with warmup and cosine decay, bf16
autocast for token models, GPT-2/PrivateProject initialisation for token models, early stopping on validation loss with the
best checkpoint kept, expert-load reporting, and exports to safetensors, npz and model.json (plus `model.kmoe` for
private_project-gen designs). `lab.autotune` searches design settings one GPU run at a time.

### Verified (2026-10-01)

* **PrivateProject-gen on the GPU:** 2 layers, 4 experts, ctx 128, PrivateProject's 4k BPE, trained on ABP's docs (436 KB). 2000 steps
  took 42 s using 0.35 GB of VRAM; validation perplexity was 121. PrivateProject's Kotlin `GenMainKt info`/`sample` loaded the
  exported checkpoint and generated from it.
* **PrivateProject registry design `model_0005`:** 128 experts, top-2, 17.1 M parameters, 0.5 M active. On PrivateProject's MNIST it
  reached 97.5% validation accuracy in 84 s. PrivateProject's registry shows 0.0 for it: it never trained on the CPU.
* **BrainBuilder's story graph:** 87% next-word validation accuracy; it round-trips back to BrainBuilder's format.

## PRIVATE_PROJECT: a model family it runs, not one it trains

PRIVATE_PROJECT (/path/redacted) is the owner's own byte-level language model: a patch encoder over raw bytes, a sparse
MoE core and a byte decoder, with a dashboard, an MCP surface and a catalog overlay (`catalog/private_project`) that
describes it to ABP without a single line being written into its checkout. `bot/neurallab/private_project.py` makes it a
Neural Lab family.

| | |
|---|---|
| `repo()` | the checkout: `$PRIVATE_PROJECT_HOME`, then what `catalog/private_project/abp-module.toml` declares (`local: /path/redacted`), then a sibling of ABP's. `None` when it is not on this machine - the lab then simply does not list it |
| `checkpoints()` | the checkpoint files, newest first, with sizes and mtimes. Resolved in `private_project/paths.py`'s own order: `$PRIVATE_PROJECT_CKPT_DIR`, then `/path/redacted` when that drive is there, then the repo's own `checkpoints/`. Both `.pt` and `.ckpt` are listed - PRIVATE_PROJECT's own `list_checkpoints` filter would hide 29 of the 34 files in `/path/redacted` |
| `configs()` / `config(name)` | the model configs as the three layers `private_project.core.config.load_config` reads (`layer1` the patcher, `layer2` the sparse core, `layer3` the thoughts), plus a summary of the numbers that identify the model (width, depth, experts, patch and thought limits). Read with PyYAML here, so showing a config needs no torch |
| `generate(...)` | one rollout: PRIVATE_PROJECT's own `python -m private_project.cli.main`, the same command `catalog/private_project`'s `generate.run` runs, under the training environment's interpreter (`<localai home>/venv-train`, where torch is), windowless and below normal priority as a `bot.sandbox_ns` `worker` cell that dies with the call. Threads capped to ABP's own (`cpu_threads()`, which moves with the stability policy). The bytes come back as a list, as hex and decoded; a rollout that fails comes back with PRIVATE_PROJECT's own error, not an exception |

Nothing here trains. `status()["trainable"]` is `false` for the same reason: a rollout only asks a
checkpoint for bytes.

```
abp lab private_project                                   # the family: how many checkpoints, which configs, is torch there
abp lab private_project-config tiny_8m                    # one config's three layers and its numbers
abp lab private_project-generate phase3.pt Once upon a time --max-new 32
abp lab private_project-generate random Once upon a time  # no checkpoint: PRIVATE_PROJECT's own freshly-initialised weights
```

API: `GET /api/lab/private_project`, `GET /api/lab/private_project/config?config=`, `POST /api/lab/private_project/generate`; the lab
overview (`GET /api/lab`) reports the family under `private_project`.

### Verified (2026-10-04)

A rollout ran from the real checkout with real torch in the training environment: `python -m private_project.cli.main`
under a `worker` cell, 16 prompt bytes plus 4 generated, the bytes decoded and hex-matched, the cell closed
when the call returned, and the sandbox registry holding one `private_project-generate:<checkpoint>` record under the
`worker` policy. Two things are true of this machine and are worth saying plainly:

* **No checkpoint on this machine loads.** `/path/redacted` carries `byte_head.*`, which
  `PrivateProjectRuntime` no longer has, and the 256-wide `e1_d256_sqrt.pt` does not fit `configs/tiny_8m.yaml`.
  That drift is PRIVATE_PROJECT's to fix (its own `checkpoint_arch_depth` helper and `tests/test_checkpoint_config.py`
  exist to catch it); ABP's job is to report it, which is what the checkpoint branch of the live test
  asserts: either it generates, or PRIVATE_PROJECT's own loader error comes back in `error` with a non-zero exit.
* **Its catalog overlay under-reports.** `catalog/private_project`'s `info.checkpoints` globs a literal
  `checkpoints/` relative to the checkout, which sees 1 of the 34 files. The lab resolves the folder the
  way PRIVATE_PROJECT's own `paths.ckpt_dir()` does.

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
* **CLI:** `abp ai …` and `abp lab …` (with the lab: `abp lab private_project`, `private_project-config`, `private_project-generate`).
* **API:** `/api/localai/*` and `/api/lab/*`.
* **MCP:** `localai_status`, `localai_discover`, `lab_status`, `lab_validate`, `lab_advice`.
* **Agent tools:** `localai_*` and `lab_*`. `lab_systune` uses per-call approval: advice runs freely; measuring and
  retraining ask.

### Per-call approval

`ToolSpec.needs_approval` may be a function of the call's input. It fails closed: no input, an exception, or a
non-bool answer all mean "ask". The function sees a copy of the input and is judged on the input as it will run,
after hooks. Plan mode and taint treat such a tool as one that changes things. Write it as an allow-list.

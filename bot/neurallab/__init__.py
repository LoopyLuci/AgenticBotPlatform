"""ABP's Neural Lab: design, train, evaluate and ship neural networks and models — one toolkit over ABP's own model
projects (KotMoE, BrainBuilder, Amethyst, Kestrion) and the system models that tune this machine.

    spec       the model description every project converts to: a graph of ops (linear, mlp, mixture of experts,
               attention, transformer blocks, embeddings, GRU, conv1d, norms, heads...), shapes inferred, parameters,
               active parameters and FLOPs counted without PyTorch
    nn_worker  builds a spec in PyTorch and trains it on the GPU (in the training environment): classification,
               regression, language models, next-token, anomaly detection; MoE balance loss; KotMoE-gen checkpoints
               read and written in KotMoE's own format
    lab        runs (start, watch, stop, compare), saved designs, design search (autotune)
    interop    BrainBuilder graphs (*.bbir.edn) <-> specs; KotMoE's registry designs, kotmoe-gen checkpoints and MNIST
               data; Amethyst module packages (.apkg) from fine-tuned LoRAs, installed with Amethyst's CLI; Kestrion
               sharing ABP's model store
    edn        BrainBuilder's EDN format
    infer      small exported models run with numpy inside ABP (microseconds per decision, no GPU)
    noema      NOEMA as a model family: its checkpoints, their configs (YAML, read here without torch), and one
               rollout from one through its own CLI as a sandbox_ns worker. Nothing here trains.
    telemetry  this machine's measurements: CPU topology (APIC ids), machine-check and power-loss history, a 10 s
               sampler of CPU, memory, disks, network and GPU into SQLite
    systune    the system models: drive transfer tuning, llama.cpp runtime options, memory forecasting, CPU stability
               guarding (which processors ABP's heavy work may use)
    service    ABP's background task: the recorder and the system models' upkeep

Fine-tuning existing language models (LoRA, DPO, GGUF export) is bot/localai/train.py; the lab covers designing and
training networks from scratch and the toolkit's own architectures.
"""

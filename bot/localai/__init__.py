"""ABP's own local AI runtime: run, serve, manage and fine-tune models on this machine — what Ollama and Unsloth do,
built into ABP, with an Ollama-compatible API so anything written for Ollama works against ABP unchanged.

    paths      where the runtime lives (ABP_LOCALAI_HOME; E:\\ABP-LocalAI on this machine, else <data>/localai)
    gguf       reads a GGUF file's metadata (architecture, size, context, quantization, chat template...) without
               loading it
    models     ABP's model store: pulled models (its own folder), imported ones (copied or hard-linked in) and
               referenced ones (used in place from another program's folder); names like "qwen2.5:0.5b"
    discover   finds models other software already has on disk: Ollama (its manifests and blobs), LM Studio, the
               Hugging Face cache, GPT4All, Jan, text-generation-webui, and any folder a person adds
    pull       downloads: Ollama's registry ("llama3.2:3b"), Hugging Face ("hf.co/<repo>:<quant>"), a URL; resumable,
               SHA-256 verified
    modelfile  Ollama's Modelfile (FROM, PARAMETER, TEMPLATE, SYSTEM, ADAPTER, MESSAGE, LICENSE): create models
    engine     llama.cpp (installed from ggml-org's releases: Vulkan, HIP/ROCm, CUDA, CPU builds); one llama-server per
               loaded model, GPU offload, keep-alive unloading, memory accounting
    server     the HTTP API: Ollama's (/api/generate, /api/chat, /api/embed, /api/pull, /api/create, /api/show,
               /api/ps, /api/tags, /api/copy, /api/delete, /api/version) and OpenAI's (/v1/chat/completions,
               /v1/completions, /v1/embeddings, /v1/models)
    train      fine-tuning (what Unsloth does): LoRA on the GPU with PyTorch (ROCm / CUDA), datasets in the common
               formats (and ABP's own logs), merge, export to GGUF, quantize, register in the store

Everything runs on this machine. Heavy work goes to the GPU; CPU threads are capped (ABP's host machine must not run
sustained all-core vector loads).
"""

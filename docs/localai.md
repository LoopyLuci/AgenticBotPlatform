# Local AI: Ollama and Unsloth inside ABP

`bot/localai` runs, serves, manages and fine-tunes language models on this machine. Everything is local: models are
on local drives, inference runs on the local GPU, and training runs on the local GPU too.

| Piece | What it does |
|---|---|
| `paths.py` | `ABP_LOCALAI_HOME`, else `E:\ABP-LocalAI` where an E: drive exists, else `<data>/localai`. `cpu_threads()` returns the CPU threads ABP's heavy work may use right now (see the Neural Lab's stability policy). |
| `models.py` | The store, laid out exactly like Ollama's: `models/manifests/<host>/<ns>/<model>/<tag>` plus `models/blobs/sha256-…`. A model is *pulled*, *imported* (hard-linked when on the same drive), or *referenced* (used in place, recorded in `abp-references.json`). Other Ollama-format stores can be read in place (`stores.json`). |
| `discover.py` | Finds models other programs keep: Ollama (`OLLAMA_MODELS`, `~/.ollama/models`, other drives), LM Studio, the Hugging Face cache, GPT4All, Jan, Kestrion (`~/.kestrion/models`), and added folders. `adopt_all()` is a new user's slot-in: Ollama stores are read in place and GGUF files are referenced, so nothing is downloaded again. |
| `pull.py` | Downloads from Ollama's registry (`qwen2.5:0.5b`) and Hugging Face (`hf.co/<org>/<repo>:<quant>`), or from a URL. Resumable and SHA-256 verified. |
| `modelfile.py` | Ollama's Modelfile: `FROM`, `PARAMETER`, `TEMPLATE`, `SYSTEM`, `ADAPTER`, `MESSAGE`, `LICENSE`. |
| `engine.py` | llama.cpp from ggml-org's releases (HIP/ROCm, Vulkan, CUDA or CPU builds, digest-checked). One `llama-server` per loaded model, with keep-alive, memory accounting and per-model options from the LLM runtime model. |
| `server.py` | Ollama's API (`/api/generate`, `chat`, `embed`, `embeddings`, `tags`, `ps`, `show`, `pull`, `create`, `copy`, `delete`, `blobs`, `version`) and OpenAI's (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models`), on **port 11436**. Kestrion's inference server keeps 11435 and Ollama 11434. A malformed request gets Ollama's `{"error": …}` with a 400. |
| `train.py`, `train_worker.py` | Fine-tuning, the Unsloth equivalent. It covers LoRA, rsLoRA, DoRA, SFT (loss on the assistant's turns only), and DPO (the reference model is the base with the adapter switched off). Data can be OpenAI, ShareGPT, Alpaca, prompt/completion, or preference JSONL, or Studio's `sft.jsonl`/`prefs.jsonl`. The run is then merged, converted to GGUF with llama.cpp's own converter, quantized, and registered in the store; the LoRA is also exported as a GGUF adapter. |
| `service.py` | ABP's background task. It keeps the server up, exports finished runs, and exports `ABP_MODELS_DIR`. |
| `tools.py` | The agents' tools: `localai_*` and `lab_*`. |

## Using it

```bash
abp ai status
```

```bash
abp ai pull hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M
```

Any Ollama client works unchanged with `OLLAMA_HOST=http://127.0.0.1:11436`. It was verified with the real `ollama`
CLI (list, run, ps, show).

Fine-tuning needs the training environment, `<home>/venv-train`. It holds AMD's PyTorch for the GPU family from
`https://repo.amd.com/rocm/whl/gfx110X-all/` (Windows and Linux; on this machine torch 2.11 with ROCm 7.13 on the
RX 7900 XTX), or the CUDA wheels with an NVIDIA card, plus transformers, peft, datasets and accelerate:

```bash
abp ai train setup
```

```bash
abp ai train start unsloth/qwen3-1.7b E:\ABP-LocalAI\datasets\abp-localai-qa.jsonl --steps 150 --name abp/qwen3-abp:v2
```

When it finishes, ABP's loop exports the run to `abp/qwen3-abp:v2` (Q4_K_M) and it can be run at once. A cached
checkpoint without its tokenizer (a partial Hugging Face download) gets just those files fetched, at the same revision.

### Verified on this machine (2026-10-01)

* **Qwen3-1.7B fine-tune:** LoRA rank 32, 150 steps, 54 s on the GPU, 4.1 GB of VRAM. After merging, GGUF conversion
  and Q4_K_M quantization (42 s on 4 threads), all six questions asked through `/api/chat` came back with exactly the
  trained answers.
* **Inference:** Qwen2.5-0.5B Q4_K_M at about 80 tok/s generation and 230 tok/s prompt. Qwen3-1.7B Q4_K_M at about 35
  and 140 tok/s.
* **Amethyst:** the run's LoRA was packaged as an Amethyst module and installed with Amethyst's CLI. Amethyst's own
  `LocalBackend` then loaded it on the GPU and answered as trained.
* **Bug found in Amethyst:** its own packager names the adapter `adapter.safetensors`, but `PeftModel.from_pretrained`
  loads `adapter_model.safetensors`. ABP's packages carry PEFT's name.

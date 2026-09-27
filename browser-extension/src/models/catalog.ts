// In-browser models (DESIGN.md section 8): a small curated catalog plus device-capability scoring, so the options page can
// recommend what will actually run on this machine instead of listing everything and letting people find out the hard way.
//
// Deliberate deviation from the original design's from-scratch download manager (resumable Range fetch, sha256 verification,
// OPFS storage, LRU eviction): both runtimes below already implement exactly that, correctly and maintained by their own
// projects - WebLLM caches compiled weights via the Cache Storage API with its own integrity checks, and transformers.js
// (onnxruntime-web underneath) does the same for ONNX models. Reimplementing that from scratch here would mean maintaining a
// second, almost certainly buggier copy of the same logic. What ABP owns instead: which model to fetch, a fit score so the
// choice is informed, and a local "has this been loaded at least once" registry (src/background/modelengine.ts) - not exact
// byte-level accounting, an honest simplification given the runtimes don't expose their cache contents for that.
export type Task = 'chat' | 'embed' | 'stt' | 'classify';
export type Runtime = 'webllm' | 'transformers';

export interface CatalogEntry {
  id: string;             // stable id used in browser-local/<id> and the options page
  name: string;
  task: Task;
  runtime: Runtime;
  repo: string;            // the MLC or Hugging Face repo id the runtime is told to load
  approx_size_mb: number;
  context?: number;
  license: string;
  requires_webgpu: boolean;
  notes: string;
}

export const CATALOG: CatalogEntry[] = [
  { id: 'qwen2.5-1.5b', name: 'Qwen2.5 1.5B Instruct (q4f16)', task: 'chat', runtime: 'webllm',
    repo: 'Qwen2.5-1.5B-Instruct-q4f16_1-MLC', approx_size_mb: 1100, context: 32768, license: 'Apache-2.0', requires_webgpu: true,
    notes: 'A good default: small, fast, tool-calling works reasonably well.' },
  { id: 'llama-3.2-1b', name: 'Llama 3.2 1B Instruct (q4f16)', task: 'chat', runtime: 'webllm',
    repo: 'Llama-3.2-1B-Instruct-q4f16_1-MLC', approx_size_mb: 880, context: 8192, license: 'Llama 3.2 Community License', requires_webgpu: true,
    notes: 'The smallest usable chat model here; good on modest hardware.' },
  { id: 'phi-3.5-mini', name: 'Phi-3.5 Mini Instruct (q4f16)', task: 'chat', runtime: 'webllm',
    repo: 'Phi-3.5-mini-instruct-q4f16_1-MLC', approx_size_mb: 2400, context: 4096, license: 'MIT', requires_webgpu: true,
    notes: 'Stronger reasoning than the 1-2B models, needs more VRAM.' },
  { id: 'bge-small', name: 'bge-small-en-v1.5 (embeddings)', task: 'embed', runtime: 'transformers',
    repo: 'Xenova/bge-small-en-v1.5', approx_size_mb: 130, license: 'MIT', requires_webgpu: false,
    notes: 'General-purpose text embeddings; runs fine on WASM alone.' },
  { id: 'whisper-tiny-en', name: 'Whisper Tiny (English, speech-to-text)', task: 'stt', runtime: 'transformers',
    repo: 'Xenova/whisper-tiny.en', approx_size_mb: 75, license: 'MIT', requires_webgpu: false,
    notes: 'Fast, English-only transcription.' },
  { id: 'whisper-base', name: 'Whisper Base (multilingual, speech-to-text)', task: 'stt', runtime: 'transformers',
    repo: 'Xenova/whisper-base', approx_size_mb: 145, license: 'MIT', requires_webgpu: false,
    notes: 'Multilingual, a bit slower than the English-only tiny model.' },
  { id: 'distilbert-sst2', name: 'DistilBERT SST-2 (sentiment classification)', task: 'classify', runtime: 'transformers',
    repo: 'Xenova/distilbert-base-uncased-finetuned-sst-2-english', approx_size_mb: 70, license: 'Apache-2.0', requires_webgpu: false,
    notes: 'Positive/negative sentiment; a stand-in for any transformers.js classifier.' },
];

export const catalogEntry = (id: string): CatalogEntry | undefined => CATALOG.find((c) => c.id === id);

export interface Capabilities {
  webgpu: boolean;
  webgpu_reason?: string;
  device_memory_gb: number | null;
  hardware_concurrency: number;
  cross_origin_isolated: boolean;
  max_storage_buffer_binding_mb: number | null;
}

/** Real probing lives in the offscreen document (it has navigator.gpu); this takes whatever it reported. Kept separate from
 * the probe itself so the scoring rule is unit-testable without a real GPU. */
export type Fit = 'good' | 'tight' | 'unsupported';

export interface FitResult { fit: Fit; reason: string }

export function fitFor(entry: CatalogEntry, caps: Capabilities): FitResult {
  if (entry.requires_webgpu && !caps.webgpu) {
    return { fit: 'unsupported', reason: caps.webgpu_reason ? `needs WebGPU (${caps.webgpu_reason})` : 'needs WebGPU, which this browser/device does not expose' };
  }
  if (entry.requires_webgpu && caps.max_storage_buffer_binding_mb !== null && caps.max_storage_buffer_binding_mb < entry.approx_size_mb * 0.9) {
    return { fit: 'unsupported', reason: `the GPU's buffer-binding limit (${Math.round(caps.max_storage_buffer_binding_mb)} MB) is smaller than this model's weights` };
  }
  const memGb = caps.device_memory_gb;
  if (memGb !== null) {
    const budgetMb = memGb * 1024 * 0.35;                                // a conservative slice of reported device memory
    if (entry.approx_size_mb > budgetMb * 1.6) return { fit: 'unsupported', reason: `this device reports ~${memGb} GB of memory, too little for a ~${entry.approx_size_mb} MB model` };
    if (entry.approx_size_mb > budgetMb) return { fit: 'tight', reason: `should load, but is a large fraction of this device's ~${memGb} GB of memory` };
  }
  return { fit: 'good', reason: entry.requires_webgpu ? 'fits comfortably on this device’s GPU' : 'runs on CPU/WASM; no GPU needed' };
}

export function rankedCatalog(caps: Capabilities, task?: Task): Array<CatalogEntry & FitResult> {
  const entries = task ? CATALOG.filter((c) => c.task === task) : CATALOG;
  const order: Record<Fit, number> = { good: 0, tight: 1, unsupported: 2 };
  return entries.map((c) => ({ ...c, ...fitFor(c, caps) })).sort((a, b) => order[a.fit] - order[b.fit] || a.approx_size_mb - b.approx_size_mb);
}

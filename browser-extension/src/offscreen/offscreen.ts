// The in-browser model engine (DESIGN.md section 8.4). Runs in a Chrome offscreen document (not the service worker, which
// cannot host a WebGPU context or a long-lived Worker the way this needs) - created lazily by src/background/modelengine.ts
// and torn down after an idle period. Talks to the background page over ordinary extension messaging.
import { CreateMLCEngine, type MLCEngine } from '@mlc-ai/web-llm';
import { pipeline, env, type FeatureExtractionPipeline } from '@huggingface/transformers';
import { catalogEntry } from '../models/catalog';

/** transformers.js has no single exported base "Pipeline" type - every task returns its own callable class. Everything here
 * that is not specifically a FeatureExtractionPipeline (embeddings) treats one as this narrow callable shape instead. */
type AnyPipeline = ((input: unknown, options?: object) => Promise<unknown>) & { dispose?: () => Promise<void> };

// onnxruntime-web's WASM binaries are copied into dist/onnx/ at build time (see esbuild.config.mjs) - never fetched from a
// CDN, consistent with every other part of this extension.
if (env.backends.onnx.wasm) env.backends.onnx.wasm.wasmPaths = chrome.runtime.getURL('onnx/');
env.allowLocalModels = false;                                            // only ever the Hugging Face hub, never a bundled path

interface Loaded { kind: 'webllm'; engine: MLCEngine; lastUsed: number }
interface LoadedPipe { kind: 'transformers'; task: string; pipe: AnyPipeline; lastUsed: number }
const loaded = new Map<string, Loaded | LoadedPipe>();
const MAX_LOADED = 2;                                                    // default 1 LLM + 1 utility model (DESIGN.md 8.4)

function evictIfNeeded(keepId: string): void {
  while (loaded.size >= MAX_LOADED && !loaded.has(keepId)) {
    let oldestId = '';
    let oldestAt = Infinity;
    for (const [id, e] of loaded) if (e.lastUsed < oldestAt) { oldestAt = e.lastUsed; oldestId = id; }
    if (!oldestId) break;
    void unload(oldestId);
  }
}

function post(req: string, patch: Record<string, unknown>): void {
  void chrome.runtime.sendMessage({ t: 'abp-engine-event', req, ...patch }).catch(() => undefined);
}

async function ensureWebLLM(id: string, req: string): Promise<MLCEngine> {
  const existing = loaded.get(id);
  if (existing?.kind === 'webllm') { existing.lastUsed = Date.now(); return existing.engine; }
  const entry = catalogEntry(id);
  if (!entry || entry.runtime !== 'webllm') throw Object.assign(new Error(`no WebLLM catalog entry ${id}`), { code: 'E_MODEL_UNAVAILABLE' });
  evictIfNeeded(id);
  const engine = await CreateMLCEngine(entry.repo, {
    initProgressCallback: (p) => post(req, { progress: { text: p.text, fraction: p.progress } }),
  });
  loaded.set(id, { kind: 'webllm', engine, lastUsed: Date.now() });
  return engine;
}

async function ensurePipeline(id: string, req: string): Promise<{ task: string; pipe: AnyPipeline }> {
  const existing = loaded.get(id);
  if (existing?.kind === 'transformers') { existing.lastUsed = Date.now(); return { task: existing.task, pipe: existing.pipe }; }
  const entry = catalogEntry(id);
  if (!entry || entry.runtime !== 'transformers') throw Object.assign(new Error(`no transformers.js catalog entry ${id}`), { code: 'E_MODEL_UNAVAILABLE' });
  evictIfNeeded(id);
  const task = entry.task === 'embed' ? 'feature-extraction' : entry.task === 'stt' ? 'automatic-speech-recognition' : 'text-classification';
  const gpu = await hasWebGPU();
  const pipe = (await pipeline(task as never, entry.repo, {
    device: gpu ? 'webgpu' : 'wasm',
    progress_callback: (p: { status: string; progress?: number }) => post(req, { progress: { text: p.status, fraction: p.progress ?? null } }),
  } as never)) as unknown as AnyPipeline;
  loaded.set(id, { kind: 'transformers', task, pipe, lastUsed: Date.now() });
  return { task, pipe };
}

async function unload(id: string): Promise<void> {
  const e = loaded.get(id);
  if (!e) return;
  loaded.delete(id);
  try { if (e.kind === 'webllm') await e.engine.unload(); else await (e.pipe as { dispose?: () => Promise<void> }).dispose?.(); }
  catch { /* best-effort */ }
}

async function hasWebGPU(): Promise<boolean> {
  try { return !!(await (navigator as { gpu?: { requestAdapter: () => Promise<unknown> } }).gpu?.requestAdapter()); } catch { return false; }
}

async function capabilities(): Promise<Record<string, unknown>> {
  const gpuNav = (navigator as { gpu?: { requestAdapter: (opts?: unknown) => Promise<{ limits?: Record<string, number>; features?: { has(n: string): boolean } } | null> } }).gpu;
  let webgpu = false;
  let reason = 'navigator.gpu is not present';
  let maxStorageMb: number | null = null;
  if (gpuNav) {
    try {
      const adapter = await gpuNav.requestAdapter();
      if (adapter) { webgpu = true; reason = ''; maxStorageMb = adapter.limits ? Math.floor((adapter.limits.maxStorageBufferBindingSize ?? 0) / (1024 * 1024)) : null; }
      else reason = 'no WebGPU adapter is available (drivers or a hardware block list)';
    } catch (e) { reason = e instanceof Error ? e.message : String(e); }
  }
  const mem = (navigator as { deviceMemory?: number }).deviceMemory;
  return {
    webgpu, webgpu_reason: reason || undefined, device_memory_gb: typeof mem === 'number' ? mem : null,
    hardware_concurrency: navigator.hardwareConcurrency || 4, cross_origin_isolated: (self as unknown as { crossOriginIsolated?: boolean }).crossOriginIsolated ?? false,
    max_storage_buffer_binding_mb: maxStorageMb,
  };
}

function normalize(vec: Float32Array | number[]): number[] {
  const arr = Array.from(vec);
  const norm = Math.sqrt(arr.reduce((s, v) => s + v * v, 0)) || 1;
  return arr.map((v) => v / norm);
}

async function handle(m: { op: string; req: string; [k: string]: unknown }): Promise<unknown> {
  switch (m.op) {
    case 'capabilities': return capabilities();
    case 'load': {
      const entry = catalogEntry(String(m.id));
      if (!entry) throw Object.assign(new Error(`unknown model ${String(m.id)}`), { code: 'E_MODEL_UNAVAILABLE' });
      if (entry.runtime === 'webllm') await ensureWebLLM(entry.id, m.req);
      else await ensurePipeline(entry.id, m.req);
      return { ok: true };
    }
    case 'unload': await unload(String(m.id)); return { ok: true };
    case 'list': return { loaded: [...loaded.keys()] };
    case 'generate': {
      const entry = catalogEntry(String(m.id));
      if (!entry) throw Object.assign(new Error(`unknown model ${String(m.id)}`), { code: 'E_MODEL_UNAVAILABLE' });
      const messages = (m.messages as Array<{ role: string; content: string }>) ?? [];
      const stream = !!m.stream;
      if (entry.runtime === 'webllm') {
        const engine = await ensureWebLLM(entry.id, m.req);
        if (stream) {
          const iter = await engine.chat.completions.create({ messages: messages as never, stream: true, max_tokens: Number(m.max_tokens) || 1024, temperature: typeof m.temperature === 'number' ? m.temperature : undefined });
          let text = '';
          for await (const chunk of iter) {
            const delta = chunk.choices?.[0]?.delta?.content ?? '';
            if (delta) { text += delta; post(m.req, { text }); }
          }
          return { text };
        }
        const r = await engine.chat.completions.create({ messages: messages as never, max_tokens: Number(m.max_tokens) || 1024, temperature: typeof m.temperature === 'number' ? m.temperature : undefined });
        return { text: r.choices?.[0]?.message?.content ?? '' };
      }
      // A transformers.js chat-ish fallback for a text-generation-task catalog entry (none shipped yet, kept for extensibility).
      const { pipe } = await ensurePipeline(entry.id, m.req);
      const prompt = messages.map((mm) => `${mm.role}: ${mm.content}`).join('\n');
      const out = (await pipe(prompt, { max_new_tokens: Number(m.max_tokens) || 256 })) as Array<{ generated_text: string }>;
      return { text: (out[0]?.generated_text ?? '').slice(prompt.length).trim() };
    }
    case 'embed': {
      const entry = catalogEntry(String(m.id));
      if (!entry || entry.task !== 'embed') throw Object.assign(new Error(`${String(m.id)} is not an embedding model`), { code: 'E_PARAMS' });
      const { pipe } = await ensurePipeline(entry.id, m.req);
      const texts = (m.texts as string[]) ?? [];
      const fe = pipe as unknown as FeatureExtractionPipeline;
      const out = await fe(texts, { pooling: 'mean', normalize: false });
      const data = (out as unknown as { tolist(): number[][] }).tolist();
      return { embeddings: data.map(normalize) };
    }
    default: throw Object.assign(new Error(`unknown engine op ${m.op}`), { code: 'E_METHOD' });
  }
}

chrome.runtime.onMessage.addListener((raw, sender, sendResponse) => {
  const m = raw as { t?: string; op?: string; req?: string };
  if (m?.t !== 'abp-engine' || sender.id !== chrome.runtime.id || typeof m.op !== 'string') return false;
  handle(m as never).then(
    (result) => sendResponse({ ok: true, result }),
    (e: unknown) => sendResponse({ ok: false, error: { code: (e as { code?: string })?.code ?? 'E_INTERNAL', message: e instanceof Error ? e.message : String(e) } }),
  );
  return true;
});

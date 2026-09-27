// Owns the offscreen document's lifecycle (created on first use, closed after being idle - a Chrome offscreen document has
// real memory/GPU cost, so it is not kept around for nothing) and the "has this model been loaded at least once" registry.
// This is the background-side half of src/offscreen/offscreen.ts; see that file for why there is no from-scratch download
// manager here.
import { BridgeError } from '../shared/protocol';
import { CATALOG, type Capabilities, catalogEntry, rankedCatalog } from '../models/catalog';
import { getLocal, setLocal } from './storage';

const REGISTRY_KEY = 'abp.models.installed';
const IDLE_CLOSE_MS = 10 * 60_000;

type Progress = { text: string; fraction: number | null };
type Waiter = { resolve: (v: unknown) => void; reject: (e: Error) => void; onProgress?: (p: Progress) => void; onDelta?: (text: string) => void };

class ModelEngine {
  private ready: Promise<void> | null = null;
  private idleTimer: ReturnType<typeof setTimeout> | null = null;
  private waiters = new Map<string, Waiter>();
  private capsCache: Capabilities | null = null;

  private async ensureOffscreen(): Promise<void> {
    if (this.ready) return this.ready;
    if (!('offscreen' in chrome)) {
      throw new BridgeError('E_MODEL_UNAVAILABLE', 'in-browser models need a Chromium browser (this browser has no offscreen-document support)');
    }
    this.ready = (async () => {
      const has = await chrome.offscreen.hasDocument?.();
      if (!has) {
        await chrome.offscreen.createDocument({
          url: 'offscreen.html',
          // "WORKERS" is the reason Chrome documents for extensions that need to run heavy WASM/WebGPU work off the
          // service worker (a true service worker cannot host a persistent WebGPU context or long-lived Worker).
          reasons: ['WORKERS' as chrome.offscreen.Reason],
          justification: 'runs in-browser AI models (WebGPU/WASM) for ABP; a service worker cannot host this',
        });
      }
    })();
    return this.ready;
  }

  private bump(): void {
    if (this.idleTimer) clearTimeout(this.idleTimer);
    this.idleTimer = setTimeout(() => { void this.close(); }, IDLE_CLOSE_MS);
  }

  async close(): Promise<void> {
    if (this.idleTimer) { clearTimeout(this.idleTimer); this.idleTimer = null; }
    if ('offscreen' in chrome && (await chrome.offscreen.hasDocument?.())) await chrome.offscreen.closeDocument().catch(() => undefined);
    this.ready = null;
  }

  private async call(op: string, params: Record<string, unknown>, opts: { onProgress?: (p: Progress) => void; onDelta?: (t: string) => void } = {}): Promise<unknown> {
    await this.ensureOffscreen();
    this.bump();
    const req = crypto.randomUUID().replace(/-/g, '');
    return new Promise((resolve, reject) => {
      this.waiters.set(req, { resolve, reject, onProgress: opts.onProgress, onDelta: opts.onDelta });
      chrome.runtime.sendMessage({ t: 'abp-engine', op, req, ...params }).then(
        (r: { ok: boolean; result?: unknown; error?: { code: string; message: string } } | undefined) => {
          this.waiters.delete(req);
          if (!r) { reject(new BridgeError('E_INTERNAL', 'the model engine did not answer')); return; }
          if (r.ok) resolve(r.result); else reject(new BridgeError((r.error?.code as never) ?? 'E_INTERNAL', r.error?.message ?? 'engine error'));
        },
        (e: unknown) => { this.waiters.delete(req); reject(e instanceof Error ? e : new Error(String(e))); },
      );
    });
  }

  /** Progress/streaming events from the offscreen document arrive as ordinary runtime messages (it has no other channel). */
  onEngineEvent(m: { req?: string; progress?: Progress; text?: string }): void {
    const w = m.req ? this.waiters.get(m.req) : undefined;
    if (!w) return;
    if (m.progress) w.onProgress?.(m.progress);
    if (typeof m.text === 'string') w.onDelta?.(m.text);
  }

  async capabilities(): Promise<Capabilities> {
    if (this.capsCache) return this.capsCache;
    const r = (await this.call('capabilities', {})) as Capabilities;
    this.capsCache = r;
    return r;
  }

  async catalog(task?: string): Promise<Array<Record<string, unknown>>> {
    let caps: Capabilities;
    try { caps = await this.capabilities(); }
    catch { caps = { webgpu: false, device_memory_gb: null, hardware_concurrency: 4, cross_origin_isolated: false, max_storage_buffer_binding_mb: null }; }
    const installed = await installedIds();
    return rankedCatalog(caps, task as never).map((c) => ({ ...c, installed: installed.includes(c.id) }));
  }

  async load(id: string, onProgress?: (p: Progress) => void): Promise<void> {
    if (!catalogEntry(id)) throw new BridgeError('E_MODEL_UNAVAILABLE', `there is no model called ${id} in ABP's catalog`);
    await this.call('load', { id }, { onProgress });
    const ids = await installedIds();
    if (!ids.includes(id)) { await setLocal(REGISTRY_KEY, [...ids, id]); onModelsChanged.fn(); }
  }

  async unload(id: string): Promise<void> { await this.call('unload', { id }); }

  async forgetInstalled(id: string): Promise<void> { await forget(id); onModelsChanged.fn(); }

  async generate(id: string, messages: Array<{ role: string; content: string }>, opts: { max_tokens?: number; temperature?: number; stream?: boolean; onDelta?: (t: string) => void } = {}): Promise<{ text: string }> {
    return (await this.call('generate', { id, messages, max_tokens: opts.max_tokens, temperature: opts.temperature, stream: !!opts.stream },
      { onDelta: opts.onDelta })) as { text: string };
  }

  async embed(id: string, texts: string[]): Promise<{ embeddings: number[][] }> {
    return (await this.call('embed', { id, texts })) as { embeddings: number[][] };
  }
}

async function installedIds(): Promise<string[]> { return getLocal<string[]>(REGISTRY_KEY, []); }
export async function forget(id: string): Promise<void> { await setLocal(REGISTRY_KEY, (await installedIds()).filter((x) => x !== id)); }
export async function installedReport(): Promise<Array<Record<string, unknown>>> {
  const ids = await installedIds();
  return ids.map((id) => { const c = catalogEntry(id); return { id, task: c?.task, runtime: c?.runtime, approx_size_mb: c?.approx_size_mb }; }).filter((x) => x.task);
}

export const engine = new ModelEngine();
export const onLlmDelta: { fn: (req: string, text: string) => void } = { fn: () => undefined };
export const onModelsChanged: { fn: () => void } = { fn: () => undefined };

chrome.runtime.onMessage.addListener((raw, sender) => {
  const m = raw as { t?: string; req?: string; progress?: Progress; text?: string };
  if (m?.t === 'abp-engine-event' && sender.id === chrome.runtime.id) engine.onEngineEvent(m);
  return false;
});

export { CATALOG };

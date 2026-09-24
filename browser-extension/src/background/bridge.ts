// The bridge client: one WebSocket to the ABP desktop app, JSON-RPC both ways (docs/browser-extension/DESIGN.md sections 3-4).
// Everything here is written to survive a service-worker restart, a flaky loopback, and a server that comes and goes.
import {
  BridgeError, PROTOCOL, errFrame, isRequest, isResponse, newId, okFrame, toBridgeError,
  type Ctx, type Frame, type RequestFrame,
} from '../shared/protocol';

export type BridgeState = 'disconnected' | 'connecting' | 'connected' | 'unpaired' | 'server-mismatch' | 'protocol-mismatch';

export interface BridgeDeps {
  url: () => Promise<string | null>;                     // where to connect (null = not paired yet)
  key: () => Promise<{ key: string; server_id: string } | null>;
  hello: () => Record<string, unknown>;                  // ext info + capabilities
  handler: (method: string, params: unknown, ctx: Ctx, signal: AbortSignal) => Promise<unknown>;
  onState: (state: BridgeState, detail?: string) => void;
  onHello: (result: Record<string, unknown>) => void;
  WebSocketImpl?: typeof WebSocket;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (t: unknown) => void;
}

export const PING_MS = 20_000;
export const DEAD_AFTER_MS = 50_000;
export const BACKOFF = { min: 500, max: 30_000 };
const IDEM_TTL_MS = 2 * 60_000;

export class BridgeClient {
  state: BridgeState = 'disconnected';
  session: string | null = null;
  private ws: WebSocket | null = null;
  private pending = new Map<string, { resolve: (v: unknown) => void; reject: (e: BridgeError) => void; timer: unknown }>();
  private inflight = new Map<string, AbortController>();
  private idem = new Map<string, { at: number; promise: Promise<unknown> }>();
  private attempt = 0;
  private wantConnected = false;
  private reconnectTimer: unknown = null;
  private pingTimer: unknown = null;
  private lastSeen = 0;
  private serverId = '';

  constructor(private d: BridgeDeps) {}

  private now = (): number => (this.d.now ?? Date.now)();
  private setTimer = (fn: () => void, ms: number): unknown => (this.d.setTimer ?? ((f, m) => setTimeout(f, m)))(fn, ms);
  private clearTimer = (t: unknown): void => (this.d.clearTimer ?? ((x) => clearTimeout(x as ReturnType<typeof setTimeout>)))(t);

  private setState(s: BridgeState, detail?: string): void {
    if (this.state === s && !detail) return;
    this.state = s;
    this.d.onState(s, detail);
  }

  /** Start (or nudge) the connection loop. Safe to call repeatedly: on alarms, on wake-up, on user action. */
  start(): void {
    this.wantConnected = true;
    if (this.state === 'connecting' || this.state === 'connected') return;
    if (this.reconnectTimer) { this.clearTimer(this.reconnectTimer); this.reconnectTimer = null; }
    void this.open();
  }

  stop(): void {
    this.wantConnected = false;
    if (this.reconnectTimer) { this.clearTimer(this.reconnectTimer); this.reconnectTimer = null; }
    this.teardown('stopped');
    this.setState('disconnected');
  }

  isConnected(): boolean { return this.state === 'connected'; }

  /** Cancel every request the server has in flight (the person pressed Stop). */
  abortInflight(): void { for (const ac of this.inflight.values()) ac.abort(); }

  private async open(): Promise<void> {
    const [url, creds] = await Promise.all([this.d.url(), this.d.key()]);
    if (!url || !creds) { this.setState('unpaired'); return; }
    this.serverId = creds.server_id;
    this.setState('connecting');
    const WS = this.d.WebSocketImpl ?? WebSocket;
    let ws: WebSocket;
    try { ws = new WS(url); } catch (e) { this.scheduleReconnect(String(e)); return; }
    this.ws = ws;
    ws.onopen = () => {
      this.lastSeen = this.now();
      this.raw({ v: 1, id: 'hello', method: 'hello', params: { protocol: [PROTOCOL], key: creds.key, ext: this.d.hello().ext, capabilities: this.d.hello().capabilities,
        ...(this.session ? { resume: { session: this.session } } : {}) } });
    };
    ws.onmessage = (ev) => { this.lastSeen = this.now(); void this.onMessage(String(ev.data), creds); };
    ws.onerror = () => { /* onclose follows */ };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.stopPing();
      this.failAllPending(new BridgeError('E_NOT_CONNECTED', 'the connection to ABP dropped', { retryable: true }));
      if (this.state === 'unpaired' || this.state === 'server-mismatch' || this.state === 'protocol-mismatch') return;
      if (ev.code === 4401) { this.setState('unpaired', 'ABP no longer recognises this browser'); return; }
      this.setState('disconnected', `closed (${ev.code})`);
      if (this.wantConnected) this.scheduleReconnect();
    };
  }

  private async onMessage(text: string, creds: { server_id: string }): Promise<void> {
    let msg: Frame;
    try { msg = JSON.parse(text) as Frame; } catch { return; }
    if (this.state === 'connecting') {                                // this must be the hello answer
      const m = msg as { id?: string; result?: Record<string, unknown>; error?: { code?: string; message?: string } };
      if (m.error) {
        const code = m.error.code;
        this.teardown('handshake refused');
        if (code === 'E_AUTH') this.setState('unpaired', m.error.message);
        else if (code === 'E_PROTOCOL') this.setState('protocol-mismatch', m.error.message);
        else { this.setState('disconnected', m.error.message); if (this.wantConnected) this.scheduleReconnect(); }
        return;
      }
      const result = m.result ?? {};
      if (String(result.server_id ?? '') !== creds.server_id) {       // trust on first use: a different server is refused
        this.teardown('server mismatch');
        this.setState('server-mismatch', 'a different ABP answered on this port');
        return;
      }
      this.session = String(result.session ?? '');
      this.attempt = 0;
      this.d.onHello(result);
      this.setState('connected');
      this.startPing();
      return;
    }
    if (isResponse(msg)) { this.onResponse(msg as { id?: string; result?: unknown; error?: { code: string; message: string; retryable?: boolean; data?: unknown; hint?: string } }); return; }
    if (isRequest(msg)) await this.onRequest(msg);
  }

  private onResponse(m: { id?: string; result?: unknown; error?: { code: string; message: string; retryable?: boolean; data?: unknown; hint?: string } }): void {
    const p = m.id ? this.pending.get(m.id) : undefined;
    if (!p || !m.id) return;
    this.pending.delete(m.id);
    this.clearTimer(p.timer);
    if (m.error) p.reject(new BridgeError((m.error.code as never) ?? 'E_INTERNAL', m.error.message, { retryable: m.error.retryable, data: m.error.data, hint: m.error.hint }));
    else p.resolve(m.result);
  }

  private async onRequest(req: RequestFrame): Promise<void> {
    if (req.method === 'cancel') {
      const id = (req.params as { id?: string } | undefined)?.id;
      if (id) this.inflight.get(id)?.abort();
      return;
    }
    if (req.method === 'ping' || req.method === 'pong') { if (req.id) this.raw(okFrame(req.id, { t: this.now() })); return; }
    const ctx = req.ctx ?? {};
    const run = (): Promise<unknown> => {
      const ac = new AbortController();
      if (req.id) this.inflight.set(req.id, ac);
      const deadline = ctx.deadline_ms ?? 30_000;
      const timeout = setTimeout(() => ac.abort(), deadline);
      return this.d.handler(req.method, req.params, ctx, ac.signal).finally(() => { clearTimeout(timeout); if (req.id) this.inflight.delete(req.id); });
    };
    // A re-sent request (after a reconnect) carries the same idempotency key: answer from the first run, never act twice.
    let promise: Promise<unknown>;
    const key = ctx.idem;
    const hit = key ? this.idem.get(key) : undefined;
    if (hit) promise = hit.promise;
    else {
      promise = run();
      if (key) this.idem.set(key, { at: this.now(), promise });
      this.sweepIdem();
    }
    if (!req.id) { promise.catch(() => undefined); return; }               // a notification: no reply
    try { this.raw(okFrame(req.id, await promise)); }
    catch (e) { this.raw(errFrame(req.id, toBridgeError(e))); }
  }

  private sweepIdem(): void {
    const cut = this.now() - IDEM_TTL_MS;
    for (const [k, v] of this.idem) if (v.at < cut) this.idem.delete(k);
  }

  /** Call the server. Rejects with a typed error on refusal, timeout or disconnect. */
  call(method: string, params: unknown = {}, timeoutMs = 30_000): Promise<unknown> {
    if (!this.ws || this.state !== 'connected') return Promise.reject(new BridgeError('E_NOT_CONNECTED', 'not connected to ABP', { retryable: true }));
    const id = newId();
    return new Promise((resolve, reject) => {
      const timer = this.setTimer(() => { this.pending.delete(id); reject(new BridgeError('E_TIMEOUT', `ABP did not answer ${method}`, { retryable: true })); }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.raw({ v: 1, id, method, params });
    });
  }

  notify(method: string, params: unknown = {}): void {
    if (this.ws && this.state === 'connected') this.raw({ v: 1, method, params });
  }

  private raw(obj: unknown): void {
    try { this.ws?.send(JSON.stringify(obj)); } catch { /* the close handler will follow */ }
  }

  private failAllPending(e: BridgeError): void {
    for (const [id, p] of this.pending) { this.clearTimer(p.timer); p.reject(e); this.pending.delete(id); }
    for (const ac of this.inflight.values()) ac.abort();
    this.inflight.clear();
  }

  private startPing(): void {
    this.stopPing();
    const tick = (): void => {
      if (this.state !== 'connected') return;
      if (this.now() - this.lastSeen > DEAD_AFTER_MS) { this.ws?.close(); return; }      // silent half-open connection
      this.raw({ v: 1, id: newId(), method: 'ping' });
      this.pingTimer = this.setTimer(tick, PING_MS);
    };
    this.pingTimer = this.setTimer(tick, PING_MS);
  }
  private stopPing(): void { if (this.pingTimer) { this.clearTimer(this.pingTimer); this.pingTimer = null; } }

  private scheduleReconnect(detail?: string): void {
    if (!this.wantConnected) return;
    const base = Math.min(BACKOFF.max, BACKOFF.min * 2 ** this.attempt);
    const delay = Math.round(base * (0.75 + Math.random() * 0.5));           // jitter
    this.attempt += 1;
    if (detail) this.d.onState(this.state, detail);
    this.reconnectTimer = this.setTimer(() => { this.reconnectTimer = null; void this.open(); }, delay);
  }

  private teardown(reason: string): void {
    this.stopPing();
    const ws = this.ws;
    this.ws = null;
    try { ws?.close(1000, reason.slice(0, 100)); } catch { /* already closed */ }
    this.failAllPending(new BridgeError('E_NOT_CONNECTED', reason, { retryable: true }));
  }
}

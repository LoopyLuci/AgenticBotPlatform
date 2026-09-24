import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BridgeClient, type BridgeDeps, type BridgeState } from '../../src/background/bridge';

class FakeWS {
  static instances: FakeWS[] = [];
  sent: any[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) { FakeWS.instances.push(this); queueMicrotask(() => this.onopen?.()); }
  send(d: string) { this.sent.push(JSON.parse(d)); }
  close(code = 1000) { queueMicrotask(() => this.onclose?.({ code })); }
  // helpers
  reply(o: unknown) { this.onmessage?.({ data: JSON.stringify(o) }); }
  drop(code = 1006) { this.onclose?.({ code }); }
}

const flush = async (n = 5) => { for (let i = 0; i < n; i++) await Promise.resolve(); await new Promise((r) => setTimeout(r, 0)); };

function make(over: Partial<BridgeDeps> = {}) {
  const states: Array<[BridgeState, string | undefined]> = [];
  const timers: Array<{ fn: () => void; ms: number }> = [];
  const handler = vi.fn(async (method: string) => ({ echoed: method }));
  const c = new BridgeClient({
    url: async () => 'ws://127.0.0.1:8787/api/browser/ws', key: async () => ({ key: 'k', server_id: 'srv' }),
    hello: () => ({ ext: { name: 'x' }, capabilities: {} }), handler,
    onState: (s, d) => states.push([s, d]), onHello: () => undefined,
    WebSocketImpl: FakeWS as unknown as typeof WebSocket,
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimer: () => undefined, ...over,
  });
  return { c, states, timers, handler };
}

const connect = async (ctx: ReturnType<typeof make>, server_id = 'srv') => {
  ctx.c.start();
  await flush();
  const ws = FakeWS.instances.at(-1)!;
  ws.reply({ v: 1, id: 'hello', result: { server_id, session: 'sess1', policy: {} } });
  await flush();
  return ws;
};

beforeEach(() => { FakeWS.instances = []; });

describe('BridgeClient', () => {
  it('sends hello with the protocol and key (not in the URL) and becomes connected', async () => {
    const ctx = make();
    const ws = await connect(ctx);
    expect(ws.url).not.toContain('k');
    expect(ws.sent[0]).toMatchObject({ method: 'hello', params: { protocol: [1], key: 'k' } });
    expect(ctx.c.state).toBe('connected');
  });

  it('refuses a different server (trust on first use) and does not retry', async () => {
    const ctx = make();
    await connect(ctx, 'someone-else');
    expect(ctx.c.state).toBe('server-mismatch');
    expect(ctx.timers.length).toBe(0);
  });

  it('stops retrying when the key is no longer accepted', async () => {
    const ctx = make();
    ctx.c.start();
    await flush();
    FakeWS.instances.at(-1)!.reply({ v: 1, id: 'hello', error: { code: 'E_AUTH', message: 'unpaired' } });
    await flush();
    expect(ctx.c.state).toBe('unpaired');
    expect(ctx.timers.length).toBe(0);
  });

  it('answers server requests and reports typed errors', async () => {
    const ctx = make();
    const ws = await connect(ctx);
    ws.reply({ v: 1, id: 'r1', method: 'tab.list', params: {}, ctx: { idem: 'i1' } });
    await flush();
    expect(ws.sent.find((m) => m.id === 'r1')).toEqual({ v: 1, id: 'r1', result: { echoed: 'tab.list' } });
  });

  it('never runs a re-sent request twice (idempotency key)', async () => {
    const ctx = make();
    const ws = await connect(ctx);
    ws.reply({ v: 1, id: 'a', method: 'tab.act', params: {}, ctx: { idem: 'same' } });
    await flush();
    ws.reply({ v: 1, id: 'b', method: 'tab.act', params: {}, ctx: { idem: 'same' } });
    await flush();
    expect(ctx.handler).toHaveBeenCalledTimes(1);
    expect(ws.sent.filter((m) => m.id === 'a' || m.id === 'b').length).toBe(2);          // both were answered
  });

  it('answers an unknown method with a typed error frame', async () => {
    const ctx = make({ handler: async () => { const { BridgeError } = await import('../../src/shared/protocol'); throw new BridgeError('E_METHOD', 'nope'); } });
    const ws = await connect(ctx);
    ws.reply({ v: 1, id: 'z', method: 'x.y', params: {} });
    await flush();
    expect(ws.sent.find((m) => m.id === 'z').error.code).toBe('E_METHOD');
  });

  it('aborts an in-flight request when the server sends cancel', async () => {
    let seen: AbortSignal | undefined;
    const ctx = make({ handler: (_m, _p, _c, signal) => { seen = signal; return new Promise(() => undefined); } });
    const ws = await connect(ctx);
    ws.reply({ v: 1, id: 'slow', method: 'tab.wait', params: {} });
    await flush();
    ws.reply({ v: 1, method: 'cancel', params: { id: 'slow' } });
    await flush();
    expect(seen?.aborted).toBe(true);
  });

  it('reconnects with growing, jittered backoff after a drop, and resumes the session', async () => {
    const ctx = make();
    const ws = await connect(ctx);
    const before = ctx.timers.length;                                            // the ping timer
    ws.drop();
    await flush();
    expect(ctx.c.state).toBe('disconnected');
    expect(ctx.timers.length).toBe(before + 1);
    const first = ctx.timers[before]!.ms;
    expect(first).toBeGreaterThanOrEqual(375);
    expect(first).toBeLessThanOrEqual(625);
    ctx.timers[before]!.fn();
    await flush();
    const ws2 = FakeWS.instances.at(-1)!;
    expect(ws2).not.toBe(ws);
    expect(ws2.sent[0].params.resume).toEqual({ session: 'sess1' });
    ws2.drop();
    await flush();
    expect(ctx.timers.at(-1)!.ms).toBeGreaterThan(first);                       // backoff grew
  });

  it('call() rejects with a typed error when not connected and when the server answers with one', async () => {
    const ctx = make();
    await expect(ctx.c.call('audit.push')).rejects.toMatchObject({ code: 'E_NOT_CONNECTED' });
    const ws = await connect(ctx);
    const p = ctx.c.call('models.report', {});
    await flush();
    const id = ws.sent.at(-1).id;
    ws.reply({ v: 1, id, error: { code: 'E_RATE_LIMITED', message: 'slow down', retryable: true } });
    await expect(p).rejects.toMatchObject({ code: 'E_RATE_LIMITED', retryable: true });
  });
});

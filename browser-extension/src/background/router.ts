// The methods ABP may call on the browser (DESIGN.md section 4.2). Every method passes through the same guard: the session is
// running, the capability is on, the tab is one ABP may use, the page is not sensitive, approval exists if the session is tainted.
import { BridgeError, type Ctx } from '../shared/protocol';
import type { Policy } from '../shared/urlpolicy';
import { classify } from '../shared/urlpolicy';
import { audit } from './audit';
import { frames as listFrames, send } from './content-rpc';
import type { Capability, Enforcer } from './policy';
import type { TabManager } from './tabs';

type Params = Record<string, unknown>;

const INTERACT_ACTIONS = new Set(['click', 'dblclick', 'rightclick', 'hover', 'focus', 'type', 'clear', 'select', 'check', 'press', 'scroll']);

export const parseRef = (ref: unknown): { frameId: number; ref: string } => {
  const s = String(ref ?? '');
  const m = /^f(\d+)\.(e\d+)$/.exec(s);
  if (m) return { frameId: Number(m[1]), ref: m[2]! };
  if (/^e\d+$/.test(s)) return { frameId: 0, ref: s };
  throw new BridgeError('E_PARAMS', 'ref must look like e12 (or f3.e12 for an iframe)');
};

export class Router {
  /** tabs with an action in flight (used to tell an agent action from a person taking over) */
  acting = new Map<number, number>();
  onUserStop: (reason: string) => void = () => undefined;

  constructor(private enforcer: Enforcer, private tabs: TabManager, private diag: () => Record<string, unknown>) {}

  async handle(method: string, params: unknown, ctx: Ctx, signal: AbortSignal): Promise<unknown> {
    const p = (params ?? {}) as Params;
    if (signal.aborted) throw new BridgeError('E_CANCELLED', 'cancelled before it started');
    const result = await this.dispatch(method, p, ctx, signal);
    if (signal.aborted) throw new BridgeError('E_CANCELLED', 'cancelled');
    return result;
  }

  private guard(cap: Capability, ctx: Ctx): void {
    this.enforcer.assertRunning();
    this.enforcer.assertCapability(cap);
    this.enforcer.assertApproved(cap, ctx);
  }

  private async tabUrl(tabId: number): Promise<string> {
    try { return (await chrome.tabs.get(tabId)).url ?? ''; } catch { throw new BridgeError('E_NO_TAB', `tab ${tabId} is gone`); }
  }

  private async dispatch(method: string, p: Params, ctx: Ctx, signal: AbortSignal): Promise<unknown> {
    switch (method) {
      case 'ping': return { ok: true, t: Date.now() };
      case 'diag.get': return this.diag();
      case 'session.start': this.enforcer.newSession(); audit('session.start', ctx.session ?? ''); return { ok: true };
      case 'session.end': await this.overlayAll('off'); audit('session.end', ctx.session ?? ''); return { ok: true };
      case 'policy.update': {
        if (p.policy) this.enforcer.setPolicy(p.policy as Partial<Policy>);
        if (typeof p.tainted === 'boolean') this.enforcer.tainted = p.tainted;
        return { ok: true, tainted: this.enforcer.tainted };
      }
      case 'tabs.list': this.enforcer.assertRunning(); return { tabs: await this.tabs.list() };
      case 'tabs.open': {
        this.guard('navigate', ctx);
        this.enforcer.rate('nav');
        const t = await this.tabs.open(String(p.url ?? ''), !!p.active);
        audit('tabs.open', new URL(t.url || 'about:blank').origin);
        await this.overlay(t.id, 'active');
        return t;
      }
      case 'tabs.close': this.guard('interact', ctx); await this.tabs.close(this.tabs.resolve(p.tab)); return { ok: true };
      case 'tabs.focus': this.enforcer.assertRunning(); await this.tabs.focus(this.tabs.resolve(p.tab)); return { ok: true };
      case 'tab.navigate': return this.navigate(p, ctx);
      case 'tab.back': case 'tab.forward': case 'tab.reload': return this.history(method, p, ctx);
      case 'tab.wait': return this.wait(p, signal);
      case 'tab.snapshot': return this.snapshot(p, ctx);
      case 'tab.text': return this.text(p, ctx);
      case 'tab.screenshot': return this.screenshot(p, ctx);
      case 'tab.act': return this.act(p, ctx);
      case 'forms.fill': return this.fillForm(p, ctx, signal);
      case 'page.find': {
        this.guard('read', ctx);
        const tab = this.tabs.resolve(p.tab);
        this.enforcer.assertReadable(await this.tabUrl(tab));
        return send(tab, { op: 'find', query: p.query });
      }
      case 'ui.overlay': { const tab = this.tabs.resolve(p.tab); await this.overlay(tab, (p.state as 'off' | 'active' | 'paused') ?? 'off', String(p.label ?? '') || undefined); return { ok: true }; }
      case 'ui.notify': {
        await chrome.notifications.create({ type: 'basic', iconUrl: 'icons/128.png', title: String(p.title ?? 'ABP').slice(0, 80), message: String(p.body ?? '').slice(0, 300) });
        return { ok: true };
      }
      case 'session.mark': audit('mark', String(p.label ?? '')); return { ok: true };
      default: throw new BridgeError('E_METHOD', `unknown method ${method}`);
    }
  }

  // -------------------------------------------------------------------------------- navigation
  private async navigate(p: Params, ctx: Ctx): Promise<unknown> {
    this.guard('navigate', ctx);
    this.enforcer.rate('nav');
    const tab = this.tabs.resolve(p.tab);
    const url = String(p.url ?? '');
    this.enforcer.assertNavigable(url);
    await chrome.tabs.update(tab, { url });
    audit('tab.navigate', safeOrigin(url));
    await this.tabs.waitLoad(tab, Number(p.timeout_ms) || 20_000).catch((e) => { if (!(e instanceof BridgeError && e.code === 'E_TIMEOUT')) throw e; });
    const t = await chrome.tabs.get(tab);
    // If the page ended up somewhere sensitive (a redirect), say so instead of letting the agent read it.
    const landed = classify(t.url ?? '', this.enforcer.policy);
    return { url: t.url, title: t.title, status: t.status, landed_sensitive: landed.sensitive, category: landed.category };
  }

  private async history(method: string, p: Params, ctx: Ctx): Promise<unknown> {
    this.guard('navigate', ctx);
    this.enforcer.rate('nav');
    const tab = this.tabs.resolve(p.tab);
    const before = await this.tabUrl(tab);
    this.enforcer.assertReadable(before);
    if (method === 'tab.reload') await chrome.tabs.reload(tab);
    else await send(tab, { op: 'history', dir: method === 'tab.forward' ? 'forward' : 'back' });
    // A history step may be a full page load or a single-page-app route change: wait for either.
    const start = Date.now();
    for (;;) {
      await sleep(120);
      const t = await chrome.tabs.get(tab);
      const moved = (t.url ?? '') !== before;
      if ((moved && t.status === 'complete') || (method === 'tab.reload' && t.status === 'complete' && Date.now() - start > 400)) break;
      if (Date.now() - start > 8000) break;
    }
    const t = await chrome.tabs.get(tab);
    if ((t.url ?? '') === before && method !== 'tab.reload') throw new BridgeError('E_PAGE_CHANGED', 'there is no page to go to in that direction', { hint: 'the tab has no such history entry' });
    return { url: t.url, title: t.title, status: t.status };
  }

  private async wait(p: Params, signal: AbortSignal): Promise<unknown> {
    this.enforcer.assertRunning();
    const tab = this.tabs.resolve(p.tab);
    const timeout = Math.min(Number(p.timeout_ms) || 10_000, 60_000);
    const what = String(p.for ?? 'load');
    const value = String(p.value ?? '');
    const start = Date.now();
    if (what === 'ms') { await sleep(Math.min(Number(p.value) || 500, timeout), signal); return { ok: true }; }
    if (what === 'load') { await this.tabs.waitLoad(tab, timeout); return { ok: true }; }
    if (what === 'idle') return send(tab, { op: 'settle', timeout_ms: timeout });
    for (;;) {
      if (signal.aborted) throw new BridgeError('E_CANCELLED', 'cancelled');
      const t = await chrome.tabs.get(tab);
      if (what === 'url' && new RegExp(value).test(t.url ?? '')) return { ok: true, url: t.url };
      if (what === 'selector' || what === 'text') {
        this.enforcer.assertReadable(t.url ?? '');
        try {
          const r = await send<{ text: string }>(tab, { op: 'text', selector: what === 'selector' ? value : undefined, max_chars: 200_000 });
          if (what === 'selector' || r.text.includes(value)) return { ok: true };
        } catch (e) { if (!(e instanceof BridgeError && (e.code === 'E_PARAMS' || e.code === 'E_PAGE_CHANGED'))) throw e; }
      }
      if (Date.now() - start > timeout) throw new BridgeError('E_TIMEOUT', `waited ${timeout} ms for ${what} ${value}`, { retryable: true });
      await sleep(200, signal);
    }
  }

  // -------------------------------------------------------------------------------- reading
  private async snapshot(p: Params, ctx: Ctx): Promise<unknown> {
    this.guard('read', ctx);
    const tab = this.tabs.resolve(p.tab);
    const url = await this.tabUrl(tab);
    this.enforcer.assertReadable(url);
    const opts = { max_elements: Number(p.max_elements) || 80, include_text: p.include_text !== false, text_chars: Number(p.text_chars) || undefined };
    const top = await send<{ elements: Array<{ ref: string }>; url: string }>(tab, { op: 'snapshot', opts }, 0);
    const out: Record<string, unknown> = { tab, ...top, origin: safeOrigin(top.url) };
    if (p.frames !== false) {
      const kids: unknown[] = [];
      for (const f of (await listFrames(tab).catch(() => [])).filter((x) => x.frameId !== 0).slice(0, 6)) {
        if (!/^https?:/.test(f.url) || classify(f.url, this.enforcer.policy).sensitive) continue;
        try {
          const s = await send<{ elements: Array<{ ref: string }> }>(tab, { op: 'snapshot', opts: { ...opts, max_elements: 40 } }, f.frameId);
          s.elements = s.elements.map((e) => ({ ...e, ref: `f${f.frameId}.${e.ref}` }));
          kids.push({ frame_id: f.frameId, url: f.url, snapshot: s });
        } catch { /* a frame that will not talk is simply omitted */ }
      }
      out.frames = kids;
    }
    audit('tab.snapshot', safeOrigin(url));
    return out;
  }

  private async text(p: Params, ctx: Ctx): Promise<unknown> {
    this.guard('read', ctx);
    const tab = this.tabs.resolve(p.tab);
    const url = await this.tabUrl(tab);
    this.enforcer.assertReadable(url);
    audit('tab.text', safeOrigin(url));
    return { ...(await send<object>(tab, { op: 'text', selector: p.selector, max_chars: Number(p.max_chars) || 20000 })), origin: safeOrigin(url) };
  }

  private async screenshot(p: Params, ctx: Ctx): Promise<unknown> {
    this.guard('read', ctx);
    const tab = this.tabs.resolve(p.tab);
    const info = await chrome.tabs.get(tab);
    this.enforcer.assertReadable(info.url ?? '');
    if (!info.active) {
      if (p.focus === false) throw new BridgeError('E_DEBUGGER_UNAVAILABLE', 'only the visible tab of a window can be captured', { hint: 'allow focusing the tab' });
      await chrome.tabs.update(tab, { active: true });
      await sleep(180);
    }
    const quality = Math.max(20, Math.min(Number(p.quality) || 60, 90));
    const dataUrl = await chrome.tabs.captureVisibleTab(info.windowId, { format: 'jpeg', quality });
    const base64 = dataUrl.slice(dataUrl.indexOf(',') + 1);
    if (base64.length > 900_000) throw new BridgeError('E_TOO_LARGE', 'the screenshot is too large for one message', { hint: 'ask for a lower quality' });
    audit('tab.screenshot', safeOrigin(info.url ?? ''));
    return { format: 'jpeg', base64, origin: safeOrigin(info.url ?? '') };
  }

  // -------------------------------------------------------------------------------- acting
  private async act(p: Params, ctx: Ctx): Promise<unknown> {
    const action = String(p.action ?? '');
    const secretFill = action === 'fill_credential';
    const isInteract = INTERACT_ACTIONS.has(action);
    if (!isInteract && !secretFill) throw new BridgeError('E_PARAMS', `unknown action ${action}`);
    const args = (p.args ?? {}) as Record<string, unknown>;
    this.guard('interact', ctx);
    if (secretFill || args.submit || (action === 'press' && String(args.key) === 'Enter')) { this.enforcer.assertCapability('forms'); this.enforcer.assertApproved('forms', ctx); }
    this.enforcer.rate('action');
    const tab = this.tabs.resolve(p.tab);
    const url = await this.tabUrl(tab);
    const v = classify(url, this.enforcer.policy);
    if (!v.allowed) throw new BridgeError('E_SENSITIVE_SITE', v.reason);
    // Login pages are "sensitive" by path, and filling a stored login there is exactly the point; nothing else is done on them.
    if (v.sensitive && !(secretFill && v.category === 'sensitive_page')) {
      throw new BridgeError('E_SENSITIVE_SITE', `${v.reason}. The agent never acts on these pages.`, { data: { category: v.category } });
    }
    const { frameId, ref } = action === 'scroll' && !p.ref ? { frameId: 0, ref: '' } : action === 'press' && !p.ref ? { frameId: 0, ref: '' } : parseRef(p.ref);
    audit(`act.${action}`, `${safeOrigin(url)} ${p.ref ?? ''}`);       // never the typed value
    this.acting.set(tab, (this.acting.get(tab) ?? 0) + 1);
    try {
      await this.overlay(tab, 'active');
      const msg: Record<string, unknown> = { op: 'act', action: secretFill ? 'fill_secret' : action, ref: ref || undefined, args };
      if (secretFill) {
        // The credential's own origin comes from ABP's vault entry; it must be exactly the page's origin. Checked here AND in the page.
        const credOrigin = String(args.credential_origin ?? '');
        if (!credOrigin) throw new BridgeError('E_PARAMS', 'fill_credential needs credential_origin');
        if (credOrigin !== safeOrigin(url)) throw new BridgeError('E_NOT_ALLOWED', 'this stored login belongs to a different site than the page in front of you');
        msg.args = { ...args, expect_origin: credOrigin, credential_origin: undefined };
      }
      const result = await send<Record<string, any>>(tab, msg, frameId);
      const t = await chrome.tabs.get(tab);
      return { ...result, tab, url: t.url, title: t.title, navigated: result.before && result.before.url !== t.url };
    } finally {
      const n = (this.acting.get(tab) ?? 1) - 1;
      if (n <= 0) this.acting.delete(tab); else this.acting.set(tab, n);
    }
  }

  private async fillForm(p: Params, ctx: Ctx, signal: AbortSignal): Promise<unknown> {
    const fields = Array.isArray(p.fields) ? (p.fields as Array<Record<string, unknown>>) : [];
    if (!fields.length || fields.length > 40) throw new BridgeError('E_PARAMS', 'fields must be a list of 1-40 entries');
    const done: unknown[] = [];
    for (const [i, f] of fields.entries()) {
      if (signal.aborted) throw new BridgeError('E_CANCELLED', 'cancelled');
      const action = f.checked !== undefined ? 'check' : f.value !== undefined && f.text === undefined ? 'select' : 'type';
      try { done.push(await this.act({ tab: p.tab, ref: f.ref, action, args: { text: f.text, value: f.value, checked: f.checked } }, ctx)); }
      catch (e) {
        if (e instanceof BridgeError) throw new BridgeError(e.code, `field ${i + 1} of ${fields.length} failed: ${e.message.replace(/^E_[A-Z_]+: /, '')}`, { retryable: e.retryable, hint: e.hint, data: { completed: done.length } });
        throw e;
      }
    }
    if (p.submit_ref) done.push(await this.act({ tab: p.tab, ref: p.submit_ref, action: 'click', args: { submit: true } }, ctx));
    return { ok: true, filled: fields.length };
  }

  // -------------------------------------------------------------------------------- overlay / stop
  async overlay(tab: number, state: 'off' | 'active' | 'paused', label?: string): Promise<void> {
    try { await send(tab, { op: 'overlay', state, label }); } catch { /* an overlay is a courtesy */ }
  }
  async overlayAll(state: 'off' | 'active' | 'paused'): Promise<void> {
    for (const t of await this.tabs.list()) await this.overlay(t.id, state);
  }
}

const safeOrigin = (url: string): string => { try { return new URL(url).origin; } catch { return url.slice(0, 40); } };

const sleep = (ms: number, signal?: AbortSignal): Promise<void> => new Promise((resolve, reject) => {
  const t = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(t); reject(new BridgeError('E_CANCELLED', 'cancelled')); }, { once: true });
});
